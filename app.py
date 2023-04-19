from ldm.generate import Generate
from ldm.invoke.restoration.realesrgan import ESRGAN
import numpy as np
import base64
from io import BytesIO
from PIL import ImageDraw, Image, ImageOps, ImageFilter, ImageChops
import cv2
import boto3
from blend_modes import multiply

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

# Inference is ran for every server call
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
    original_image_mask_copy = clean_noise(original_image_mask)
    imgs = []
    for i in range(n_imgs):
        img = img2img_main(
            model,
            prompt,
            image_with_alpha_transparency,
            final_bw_mask,
            original_image_mask
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

def clean_noise(img):
    array_img = np.array(img.copy())
    kernel = np.ones((3, 3), np.uint8)
    noise_reduction = cv2.morphologyEx(array_img, cv2.MORPH_CLOSE, kernel)
    noise_reduction = cv2.morphologyEx(noise_reduction, cv2.MORPH_OPEN, kernel)
    return Image.fromarray(noise_reduction)

def img2img_main(
    model,
    prompt,
    image_with_alpha_transparency,
    final_bw_mask, 
    original_image_mask):
    """
    Main function for performing img2img with masks in the manner
    we want to process our images.
    """
    image_with_alpha_transparency = add_shadow(original_image_mask, image_with_alpha_transparency)
    final_image = get_raw_generation(model, prompt, image_with_alpha_transparency,
                                     original_image_mask.filter(ImageFilter.GaussianBlur(5)), 0, 0)
    final_image = final_image.convert("RGB")
    return final_image

def get_raw_generation(gr, prompt, image_with_alpha_transparency, init_image_mask, ss=0, sb=0):
    n = 5
    init_strength = 0.65
    init_seam_strength = 0.5
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

def get_mask_from_diff(img_diff):
    img = np.array(img_diff)
    # Find contours and hierarchy in the image
    contours, hierarchy = cv2.findContours(img, cv2.RETR_TREE, cv2.CHAIN_APPROX_NONE)
    drawing = []
    for i,c in enumerate(contours):
        # fill all contours (don't know how right this is)
        drawing.append(c)
    ### thickness might be very problematic below
    img = cv2.drawContours(img, drawing, -1, (255,255,255), thickness=1)
    img = Image.fromarray(img)
    return img

def prepare_masks_differencing_main(original_image, bg, output_mask_address=None):
    diff = ImageChops.difference(original_image, bg)
    # remove rough edges (might be very problematic)
    diff_bw = diff.convert("L").point(lambda x: 0 if x<5 else x)
    diff_bw = diff_bw.point(lambda x: 255 if x>0 else x) # binarize the difference mask
    diff_mask = get_mask_from_diff(diff_bw)
    
    alpha_transparent_mask, final_bw_mask = get_masks(diff_mask)
    
    final_image_with_alpha_transparency = original_image.copy()
    final_image_with_alpha_transparency.putalpha(alpha_transparent_mask)

    # resizing
    final_bw_mask = final_bw_mask.resize((512, 512))
    final_image_with_alpha_transparency = final_image_with_alpha_transparency.resize((512, 512))

    return final_image_with_alpha_transparency, final_bw_mask, diff_mask

def get_masks(diff_mask): 
    # expand the current mask
    bloated_mask = diff_mask.filter(ImageFilter.GaussianBlur(5))
    bloated_mask = bloated_mask.point(
        lambda x: 255 if x!=0 else 0
    )
    
    # create alpha transparent mask
    alpha_transparency = 0.9
    new_mask = bloated_mask.point(
        lambda x: int(x+alpha_transparency*255)
        )
    
    # blur the new mask
    new_mask = new_mask.filter(ImageFilter.GaussianBlur(5))
    
    # preserve the product details
    new_mask.paste(diff_mask, (0,0), diff_mask)
    
    alpha_transparent_mask = new_mask
    final_bw_mask = new_mask.point(lambda x: 255 if x>alpha_transparency*255 else 0)
    
    return alpha_transparent_mask, final_bw_mask

def add_shadow(original_image_mask, composite_image):
    """
    Add shadow to the composite image.
    """
    # create shadow image
    shadow = get_shadow(original_image_mask)

    # get only shadow portion that isn't covering the original product
    kernel = np.ones((3, 3), np.uint8)
    erosion = cv2.erode(np.array(original_image_mask),
                       kernel,
                       iterations = 3)
    original_image_mask_inner = Image.fromarray(erosion).filter(ImageFilter.GaussianBlur(10))
    shadow.paste(original_image_mask_inner, mask=original_image_mask_inner)
    
    # blend the image with the shadow
    new_composite_image = composite_image.copy()
    new_composite_image.putalpha(Image.new("L", (composite_image.size[0], composite_image.size[1]), 255))
    background_img_float = np.array(new_composite_image).astype(float)
    foreground_img_float = np.array(shadow).astype(float)

    blended_img_float = multiply(background_img_float, foreground_img_float, 0.5)

    blended_img = np.uint8(blended_img_float)
    blended_img_raw = Image.fromarray(blended_img)
    
    return blended_img_raw

def get_shadow(mask):
    """
    Create shadow layer using give mask
    """
    # expanding the image to avoid blackening of pixels at the edges
    shadow = ImageOps.expand(mask, border=300, fill='black')
    
    # actual shadow creation
    alpha_blur = shadow.filter(ImageFilter.GaussianBlur(
        np.random.randint(3,8)
    ))
    shadow = ImageOps.invert(alpha_blur)
    
    # cropping to get position for the original image back
    shadow = ImageOps.crop(shadow, 300)
    
    # adjust the shadows position by pasting it with an offset on a new image
    offset = np.random.randint(3, 8), np.random.randint(3, 8)
    new_backdrop = Image.new("RGB", (shadow.width, shadow.height), color=(255, 255, 255))
    new_backdrop.paste(shadow, offset)
    
    # # create mask for shadow
    new_mask = Image.new("L", (shadow.width, shadow.height), color=255)
    new_backdrop.putalpha(new_mask)
        
    return new_backdrop

def img_to_base64_str(img):
    buffered = BytesIO()
    img.save(buffered, format="PNG")
    buffered.seek(0)
    img_byte = buffered.getvalue()
    img_str = "data:image/png;base64," + base64.b64encode(img_byte).decode()
    return img_str

def api_to_img(img):
    """
    Converts the api string into image.
    """
    try:
        respImage = base64.b64decode(img.split(',')[1])
    except:
        respImage = base64.b64decode(img)
    respImage = BytesIO(respImage)
    respImage = Image.open(respImage)
    return respImage

# S3 Utils ------------------------------------

def load_images(composite_image, bg_image, access_key, secret_key):
    s3_client = boto3.client(
        's3',
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key, 
        region_name="ap-south-1"
    )
    download_file(s3_client, "Composites/"+composite_image+".png")
    download_file(s3_client, "Backgrounds/"+bg_image+".png")
    composite_image, bg_image = Image.open(composite_image+".png"), Image.open(bg_image+".png")
    return s3_client, composite_image, bg_image

def download_file(client, path, bucket_name='fotomaker'):
    client.download_file(bucket_name, path, path.split("/")[-1])
    return None

def save_images(save_name, imgs, client):
    path_exists, start_i = check_path_exists(client, save_name)
    
    keys = []
    for idx, img in enumerate(imgs):
        key = f"GeneratedImages/{save_name}/{start_i+idx}.png"
        save_response_s3(
            client,
            img,
            key
            )
        keys.append(key)
    return keys

def check_path_exists(client, folder_name):
    count_objs = client.list_objects_v2(
        Bucket='fotomaker',
        Prefix="GeneratedImages/"+folder_name)['KeyCount']
    if count_objs==0:
        return False, 1
    else:
        return True, count_objs+1

def save_response_s3(client, file, key):
    in_mem_file = BytesIO()
    file.save(in_mem_file, format="PNG")
    in_mem_file.seek(0)
    
    client.upload_fileobj(in_mem_file, 'fotomaker', key)
    return None

def get_urls(client, keys):
    img_urls = []
    for key in keys:
        url = create_presigned_url(client, key)
        img_urls.append(url)
    return img_urls

def create_presigned_url(client, key, expiration=60*5):
    # Generate a presigned URL for the S3 object
    response = client.generate_presigned_url(
        'get_object',
        Params={'Bucket': 'fotomaker','Key': key},
        ExpiresIn=expiration)
    return response

# Upscaling Utils ------------------------------------

def upscale_images(img_list):
    """
    Upscale all the images in the above list of images
    """
    final_images = []
    upscaler = ESRGAN()
    for img in img_list:
        final_images.append(upscale_image(upscaler, img))

    return final_images

def upscale_image(upscaler, img):
    """
    Upscale a single image given the upscaler and image
    """
    output_img = upscaler.process(img, 0.6, 0, 2)
    return output_img

