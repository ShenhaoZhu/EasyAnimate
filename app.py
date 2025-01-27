import time 

from easyanimate.api.api import infer_forward_api, update_diffusion_transformer_api, update_edition_api
from easyanimate.ui.ui import ui_modelscope, ui_eas, ui

if __name__ == "__main__":
    # Choose the ui mode  
    ui_mode = "normal"
    
    # Low gpu memory mode, this is used when the GPU memory is under 16GB
    low_gpu_memory_mode = False
    # Server ip
    server_name = "0.0.0.0"
    server_port = 7860

    # Params below is used when ui_mode = "modelscope"
    edition = "v3"
    config_path = "config/easyanimate_video_slicevae_motion_module_v3.yaml"
    model_name = "models/Diffusion_Transformer/EasyAnimateV3-XL-2-InP-512x512"
    savedir_sample = "samples"

    if ui_mode == "modelscope":
        demo, controller = ui_modelscope(edition, config_path, model_name, savedir_sample, low_gpu_memory_mode)
    elif ui_mode == "eas":
        demo, controller = ui_eas(edition, config_path, model_name, savedir_sample)
    else:
        demo, controller = ui(low_gpu_memory_mode)

    # launch gradio
    app, _, _ = demo.queue(status_update_rate=1).launch(
        server_name=server_name,
        server_port=server_port,
        prevent_thread_lock=True
    )
    
    # launch api
    infer_forward_api(None, app, controller)
    update_diffusion_transformer_api(None, app, controller)
    update_edition_api(None, app, controller)
    
    # not close the python
    while True:
        time.sleep(5)