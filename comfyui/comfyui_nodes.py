import gc
import os

import torch
import numpy as np
from PIL import Image
from diffusers import (AutoencoderKL, DDIMScheduler,
                       DPMSolverMultistepScheduler,
                       EulerAncestralDiscreteScheduler, EulerDiscreteScheduler,
                       PNDMScheduler)
from einops import rearrange
from omegaconf import OmegaConf
from transformers import CLIPImageProcessor, CLIPVisionModelWithProjection

import comfy.model_management as mm
import folder_paths
from comfy.utils import ProgressBar, load_torch_file

from ..easyanimate.models.autoencoder_magvit import AutoencoderKLMagvit
from ..easyanimate.models.transformer3d import Transformer3DModel
from ..easyanimate.pipeline.pipeline_easyanimate_inpaint import EasyAnimateInpaintPipeline
from ..easyanimate.utils.utils import get_image_to_video_latent
from ..easyanimate.data.bucket_sampler import ASPECT_RATIO_512, get_closest_ratio

# Compatible with Alibaba EAS for quick launch
eas_cache_dir       = '/stable-diffusion-cache/models'
# The directory of the easyanimate
script_directory    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def tensor2pil(image):
    return Image.fromarray(np.clip(255. * image.cpu().numpy(), 0, 255).astype(np.uint8))

def numpy2pil(image):
    return Image.fromarray(np.clip(255. * image, 0, 255).astype(np.uint8))

def to_pil(image):
    if isinstance(image, Image.Image):
        return image
    if isinstance(image, torch.Tensor):
        return tensor2pil(image)
    if isinstance(image, np.ndarray):
        return numpy2pil(image)
    raise ValueError(f"Cannot convert {type(image)} to PIL.Image")

class LoadEasyAnimateModel:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": (
                    [ 
                        'EasyAnimateV3-XL-2-InP-512x512',
                        'EasyAnimateV3-XL-2-InP-768x768',
                        'EasyAnimateV3-XL-2-InP-960x960'
                    ],
                    {
                        "default": 'EasyAnimateV3-XL-2-InP-768x768',
                    }
                ),
                "low_gpu_memory_mode":(
                    [False, True],
                    {
                        "default": False,
                    }
                ),
                "config": (
                    [
                        "easyanimate_video_slicevae_motion_module_v3.yaml",
                    ],
                    {
                        "default": "easyanimate_video_slicevae_motion_module_v3.yaml",
                    }
                ),
                "precision": (
                    ['fp16', 'bf16'],
                    {
                        "default": 'bf16'
                    }
                ),
                
            },
        }

    RETURN_TYPES = ("EASYANIMATESMODEL",)
    RETURN_NAMES = ("easyanimate_model",)
    FUNCTION = "loadmodel"
    CATEGORY = "EasyAnimateWrapper"

    def loadmodel(self, low_gpu_memory_mode, model, precision, config):
        # Init weight_dtype and device
        device          = mm.get_torch_device()
        offload_device  = mm.unet_offload_device()
        weight_dtype = {"bf16": torch.bfloat16, "fp16": torch.float16, "fp32": torch.float32}[precision]

        # Init processbar
        pbar = ProgressBar(4)

        # Load config
        config_path = f"{script_directory}/config/{config}"
        config = OmegaConf.load(config_path)

        # Detect model is existing or not 
        model_path = os.path.join(folder_paths.models_dir, "EasyAnimate", model)
      
        if not os.path.exists(model_path):
            if os.path.exists(eas_cache_dir):
                model_path = os.path.join(eas_cache_dir, 'EasyAnimate', model)
            else:
                print(f"Please download easyanimate model to: {model_path}")

        # Load vae
        if OmegaConf.to_container(config['vae_kwargs'])['enable_magvit']:
            Choosen_AutoencoderKL = AutoencoderKLMagvit
        else:
            Choosen_AutoencoderKL = AutoencoderKL
        print("Load Vae.")
        vae = Choosen_AutoencoderKL.from_pretrained(
            model_path, 
            subfolder="vae", 
        ).to(weight_dtype)
        # Update pbar
        pbar.update(1)

        # Load Sampler
        print("Load Sampler.")
        scheduler = EulerDiscreteScheduler.from_pretrained(model_path, subfolder= 'scheduler')
        # Update pbar
        pbar.update(1)
        
        # Load Transformer
        print("Load Transformer.")
        transformer = Transformer3DModel.from_pretrained(
            model_path, 
            subfolder= 'transformer', 
            transformer_additional_kwargs=OmegaConf.to_container(config['transformer_additional_kwargs'])
        ).to(weight_dtype).eval()  
        # Update pbar
        pbar.update(1) 

        # Load Transformer
        if transformer.config.in_channels == 12:
            clip_image_encoder = CLIPVisionModelWithProjection.from_pretrained(
                model_path, subfolder="image_encoder"
            ).to(device, weight_dtype)
            clip_image_processor = CLIPImageProcessor.from_pretrained(
                model_path, subfolder="image_encoder"
            )
        else:
            clip_image_encoder = None
            clip_image_processor = None   
        # Update pbar
        pbar.update(1)

        pipeline = EasyAnimateInpaintPipeline.from_pretrained(
                model_path,
                transformer=transformer,
                scheduler=scheduler,
                vae=vae,
                torch_dtype=weight_dtype,
                clip_image_encoder=clip_image_encoder,
                clip_image_processor=clip_image_processor,
        )
    
        if low_gpu_memory_mode:
            pipeline.enable_sequential_cpu_offload()
        else:
            pipeline.enable_model_cpu_offload()

        easyanimate_model = {
            'pipeline': pipeline, 
            'dtype': weight_dtype,
            'model_path': model_path,
        }
        return (easyanimate_model,)


class TextBox:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "prompt": ("STRING", {"multiline": True, "default": "",}),
            }
        }
    
    RETURN_TYPES = ("STRING_PROMPT",)
    RETURN_NAMES =("prompt",)
    FUNCTION = "process"
    CATEGORY = "EasyAnimateWrapper"

    def process(self, prompt):
        return (prompt, )


class EasyAnimateI2VSampler:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "easyanimate_model": (
                    "EASYANIMATESMODEL", 
                ),
                "prompt": (
                    "STRING_PROMPT",
                ),
                "negative_prompt": (
                    "STRING_PROMPT",
                ),
                "video_length": (
                    "INT", {"default": 72, "min": 8, "max": 144, "step": 8}
                ),
                "base_resolution": (
                    [ 
                        512,
                        768,
                        960,
                    ], {"default": 768}
                ),
                "seed": (
                    "INT", {"default": 43, "min": 0, "max": 0xffffffffffffffff}
                ),
                "steps": (
                    "INT", {"default": 25, "min": 1, "max": 200, "step": 1}
                ),
                "cfg": (
                    "FLOAT", {"default": 7.0, "min": 1.0, "max": 20.0, "step": 0.01}
                ),
                "scheduler": (
                    [ 
                        "Euler",
                        "Euler A",
                        "DPM++",
                        "PNDM",
                        "DDIM",
                    ],
                    {
                        "default": 'Euler'
                    }
                )
            },
            "optional":{
                "start_img": ("IMAGE",),
                "end_img": ("IMAGE",),
            },
        }
    
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES =("images",)
    FUNCTION = "process"
    CATEGORY = "EasyAnimateWrapper"

    def process(self, easyanimate_model, prompt, negative_prompt, video_length, base_resolution, seed, steps, cfg, scheduler, start_img=None, end_img=None):
        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()

        mm.soft_empty_cache()
        gc.collect()

        start_img = [to_pil(_start_img) for _start_img in start_img] if start_img is not None else None
        end_img = [to_pil(_end_img) for _end_img in end_img] if end_img is not None else None
        # Count most suitable height and width
        aspect_ratio_sample_size    = {key : [x / 512 * base_resolution for x in ASPECT_RATIO_512[key]] for key in ASPECT_RATIO_512.keys()}
        original_width, original_height = start_img[0].size if type(start_img) is list else Image.open(start_img).size
        closest_size, closest_ratio = get_closest_ratio(original_height, original_width, ratios=aspect_ratio_sample_size)
        height, width = [int(x / 16) * 16 for x in closest_size]
        
        # Get Pipeline
        pipeline = easyanimate_model['pipeline']
        model_path = easyanimate_model['model_path']

        # Load Sampler
        if scheduler == "DPM++":
            noise_scheduler = DPMSolverMultistepScheduler.from_pretrained(model_path, subfolder= 'scheduler')
        elif scheduler == "Euler":
            noise_scheduler = EulerDiscreteScheduler.from_pretrained(model_path, subfolder= 'scheduler')
        elif scheduler == "Euler A":
            noise_scheduler = EulerAncestralDiscreteScheduler.from_pretrained(model_path, subfolder= 'scheduler')
        elif scheduler == "PNDM":
            noise_scheduler = PNDMScheduler.from_pretrained(model_path, subfolder= 'scheduler')
        elif scheduler == "DDIM":
            noise_scheduler = DDIMScheduler.from_pretrained(model_path, subfolder= 'scheduler')
        pipeline.scheduler = noise_scheduler

        generator= torch.Generator(device).manual_seed(seed)

        with torch.no_grad():
            video_length = int(video_length // pipeline.vae.mini_batch_encoder * pipeline.vae.mini_batch_encoder) if video_length != 1 else 1
            input_video, input_video_mask, clip_image = get_image_to_video_latent(start_img, end_img, video_length=video_length, sample_size=(height, width))

            sample = pipeline(
                prompt, 
                video_length = video_length,
                negative_prompt = negative_prompt,
                height      = height,
                width       = width,
                generator   = generator,
                guidance_scale = cfg,
                num_inference_steps = steps,

                video        = input_video,
                mask_video   = input_video_mask,
                clip_image   = clip_image, 
                comfyui_progressbar = True,
            ).videos
            videos = rearrange(sample, "b c t h w -> (b t) h w c")
        return (videos,)   


class EasyAnimateT2VSampler:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "easyanimate_model": (
                    "EASYANIMATESMODEL", 
                ),
                "prompt": (
                    "STRING_PROMPT", 
                ),
                "negative_prompt": (
                    "STRING_PROMPT", 
                ),
                "video_length": (
                    "INT", {"default": 72, "min": 8, "max": 144, "step": 8}
                ),
                "width": (
                    "INT", {"default": 1008, "min": 64, "max": 2048, "step": 64}
                ),
                "height": (
                    "INT", {"default": 576, "min": 64, "max": 2048, "step": 64}
                ),
                "is_image":(
                    [
                        False,
                        True
                    ], 
                    {
                        "default": False,
                    }
                ),
                "seed": (
                    "INT", {"default": 43, "min": 0, "max": 0xffffffffffffffff}
                ),
                "steps": (
                    "INT", {"default": 25, "min": 1, "max": 200, "step": 1}
                ),
                "cfg": (
                    "FLOAT", {"default": 7.0, "min": 1.0, "max": 20.0, "step": 0.01}
                ),
                "scheduler": (
                    [ 
                        "Euler",
                        "Euler A",
                        "DPM++",
                        "PNDM",
                        "DDIM",
                    ],
                    {
                        "default": 'Euler'
                    }
                ),
            },
        }
    
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES =("images",)
    FUNCTION = "process"
    CATEGORY = "EasyAnimateWrapper"

    def process(self, easyanimate_model, prompt, negative_prompt, video_length, width, height, is_image, seed, steps, cfg, scheduler):
        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()

        mm.soft_empty_cache()
        gc.collect()

        # Get Pipeline
        pipeline = easyanimate_model['pipeline']
        model_path = easyanimate_model['model_path']

        # Load Sampler
        if scheduler == "DPM++":
            noise_scheduler = DPMSolverMultistepScheduler.from_pretrained(model_path, subfolder= 'scheduler')
        elif scheduler == "Euler":
            noise_scheduler = EulerDiscreteScheduler.from_pretrained(model_path, subfolder= 'scheduler')
        elif scheduler == "Euler A":
            noise_scheduler = EulerAncestralDiscreteScheduler.from_pretrained(model_path, subfolder= 'scheduler')
        elif scheduler == "PNDM":
            noise_scheduler = PNDMScheduler.from_pretrained(model_path, subfolder= 'scheduler')
        elif scheduler == "DDIM":
            noise_scheduler = DDIMScheduler.from_pretrained(model_path, subfolder= 'scheduler')
        pipeline.scheduler = noise_scheduler

        generator= torch.Generator(device).manual_seed(seed)
        
        video_length = 1 if is_image else video_length
        with torch.no_grad():
            video_length = int(video_length // pipeline.vae.mini_batch_encoder * pipeline.vae.mini_batch_encoder) if video_length != 1 else 1
            input_video, input_video_mask, clip_image = get_image_to_video_latent(None, None, video_length=video_length, sample_size=(height, width))
            sample = pipeline(
                prompt, 
                video_length = video_length,
                negative_prompt = negative_prompt,
                height      = height,
                width       = width,
                generator   = generator,
                guidance_scale = cfg,
                num_inference_steps = steps,

                video        = input_video,
                mask_video   = input_video_mask,
                clip_image   = clip_image, 
                comfyui_progressbar = True,
            ).videos
            videos = rearrange(sample, "b c t h w -> (b t) h w c")
        return (videos,)   


NODE_CLASS_MAPPINGS = {
    "LoadEasyAnimateModel": LoadEasyAnimateModel,
    "TextBox": TextBox,
    "EasyAnimateI2VSampler": EasyAnimateI2VSampler,
    "EasyAnimateT2VSampler": EasyAnimateT2VSampler,
}


NODE_DISPLAY_NAME_MAPPINGS = {
    "TextBox": "TextBox",
    "LoadEasyAnimateModel": "Load EasyAnimate Model",
    "EasyAnimateI2VSampler": "EasyAnimate Sampler for Image to Video",
    "EasyAnimateT2VSampler": "EasyAnimate Sampler for Text to Video",
}