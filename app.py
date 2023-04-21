from ldm.generate import Generate
import numpy as np
from PIL import ImageFilter
import cv2
from preprocessing_utils import *
from s3_utils import *
from upscaling_utils import *

# Init is ran on server startup
# Load your model to GPU as a global variable here using the variable name "model"
def init():
    global model
    # Create an object with default values
    model = Generate(
        model='stable-diffusion-1.5',
        conf='/workspace/configs/models.yaml.example',
        sampler_name ='ddim'
        )

    # do the slow model initialization
    model.load_model()

# Inference is run for every server call
# Reference your preloaded global model variable here.
def inference(model_inputs:dict) -> dict:
    global model

    # Parse out your arguments
    prompt = model_inputs.get("prompt")
    n_imgs = model_inputs.get("n_imgs")
    composite_image_name = model_inputs.get("composite_image")
    bg_image_name = model_inputs.get('bg_image')
    
    client, composite_image, bg_image = load_images(
        composite_image_name,
        bg_image_name,
        model_inputs["access_key"],
        model_inputs["secret_key"]
        )
    image_with_alpha_transparency, final_bw_mask, original_image_mask = prepare_masks_differencing_main(composite_image,
                                                                                                        bg_image,
                                                                                                        None)
    alpha_mask = image_with_alpha_transparency.getchannel('A')
    faded_mask = get_faded_black_image(original_image_mask)

    imgs = []
    for i in range(n_imgs):
        img = img2img_main(
            model,
            prompt,
            image_with_alpha_transparency,
            final_bw_mask,
            original_image_mask,
            faded_mask
            )
        imgs.append(img)

    # upscaling the images
    imgs = upscale_images(imgs)
    
    # saving the images
    keys = save_images(composite_image_name, imgs, client)
    img_urls = get_urls(client, keys)

    return {'generatedImages': img_urls}

###---###---###---###---###---###---###---###---###---###---###---###---###---###---###---###---###---###---
### util functions

def img2img_main(
    model,
    prompt,
    image_with_alpha_transparency,
    final_bw_mask, 
    original_image_mask,
    faded_mask):
    """
    Main function for performing img2img with masks in the manner
    we want to process our images.
    """
    image_with_alpha_transparency = add_shadow(
        original_image_mask,
        image_with_alpha_transparency,
        'no_offset'
        )
    final_image = get_raw_generation(
        model, 
        prompt,
        image_with_alpha_transparency,
        faded_mask, 
        0, 
        0
        )
    final_image = final_image.convert("RGB")
    return final_image

def get_raw_generation(gr, prompt, image_with_alpha_transparency, init_image_mask, ss=0, sb=0):
    n = 3
    init_strength = 0.65
    init_seam_strength = 0
    curr_image = None

    alpha_mask = image_with_alpha_transparency.getchannel('A')
    
    for i in range(n):
        if curr_image:
            curr_image.putalpha(alpha_mask)
            curr_strength = np.max([init_strength*0.8, 0.15])
            curr_seam_strength = np.max([init_seam_strength*0.8, 0.15])
        else:
            curr_image = image_with_alpha_transparency
            curr_strength = init_strength
            curr_seam_strength = init_seam_strength

        results = gr.prompt2image(
            prompt = prompt,
            outdir = "./",
            steps = 50,
            init_img = curr_image,
            init_mask = init_image_mask,
            strength = curr_strength,
            cfg_scale = 8.5,
            iterations = 1,
            seed=None,
            mask_blur_radius=0,
            seam_size= ss, 
            seam_blur= sb,
            seam_strength = curr_seam_strength,
            seam_steps= 50,
        )

        curr_image = results[0][0]
        
    return curr_image