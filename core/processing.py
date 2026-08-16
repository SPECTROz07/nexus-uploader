# -*- coding: utf-8 -*-

import os
import re
import math
import logging
from pathlib import Path
from io import BytesIO
from PIL import Image, ImageFile

from core.config import MAX_IMAGE_HEIGHT, PASS_THROUGH_EXTENSIONS

logger = logging.getLogger(__name__)

ImageFile.LOAD_TRUNCATED_IMAGES = True

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    logging.warning("pillow_heif não encontrado. Suporte a imagens .heic/.heif desativado.")


def validate_and_truncate(text, max_length):
    if text is None: return None
    text_str = str(text)
    if len(text_str) > max_length:
        logging.warning(f"Texto '{text_str}' excedeu o limite de {max_length} caracteres e foi truncado.")
        return text_str[:max_length]
    return text_str

def process_image(image_path):
    file_path = Path(image_path)
    base_filename, ext = file_path.stem, file_path.suffix.lower()

    if ext in PASS_THROUGH_EXTENSIONS:
        with open(file_path, 'rb') as f:
            return [(file_path.name, f.read())]

    with Image.open(file_path) as img:
        img_without_exif = Image.new(img.mode, img.size)
        img_without_exif.putdata(list(img.getdata()))

        if img_without_exif.mode not in ['RGB', 'RGBA']:
            img_without_exif = img_without_exif.convert('RGB')

        width, height = img_without_exif.size
        buffers = []
        save_params = {'format': 'WEBP', 'quality': 95}

        if height > MAX_IMAGE_HEIGHT:
            num_splits = math.ceil(height / MAX_IMAGE_HEIGHT)
            for j in range(num_splits):
                top = j * MAX_IMAGE_HEIGHT
                bottom = min((j + 1) * MAX_IMAGE_HEIGHT, height)
                cropped_img = img_without_exif.crop((0, top, width, bottom))
                buffer = BytesIO()
                cropped_img.save(buffer, **save_params)
                buffer.seek(0)
                filename = f"{base_filename}_part{j+1}.webp"
                buffers.append((filename, buffer.getvalue()))
        else:
            buffer = BytesIO()
            img_without_exif.save(buffer, **save_params)
            buffer.seek(0)
            filename = f"{base_filename}.webp"
            buffers.append((filename, buffer.getvalue()))
        return buffers

def extract_chapter_number_py(text):
    if not text:
        return None

    filename = os.path.basename(text).replace(',', '.')

    # Padrão 1: Tenta encontrar o número logo após uma palavra-chave de capítulo.
    # Aprimorado para aceitar espaço, hífen ou underscore.
    pattern = r'(?:cap|capitulo|capítulo|chapter|ch)\.?[\s_-]*(\d+\.?\d*)'
    match = re.search(pattern, filename, re.IGNORECASE)
    
    if match:
        number_str = match.group(1)
    else:
        # Padrão 2 (Fallback): Procura por todos os números e pega o último.
        matches = re.findall(r'(\d+\.?\d+)', filename)
        if not matches:
            return None
        number_str = matches[-1]

    # Normaliza o número para remover ".0" desnecessários.
    try:
        num = float(number_str)
        return str(int(num)) if num.is_integer() else str(num)
    except ValueError:
        return number_str

def natural_sort_key(s):
    return [int(text) if text.isdigit() else text.lower() for text in re.split('([0-9]+)', s)]

def process_image_from_bytes(filename, file_bytes, quality=95, lossless=False):
    """
    Processa os bytes de uma imagem que já está na memória.
    Converte para WebP e retorna o novo nome e os novos bytes.
    Permite modo lossless para qualidade máxima.
    """
    try:
        image_stream = BytesIO(file_bytes)
        image_stream.seek(0)
        
        with Image.open(image_stream) as img:
            img.load()

            # Remove metadados EXIF que podem causar problemas
            if 'exif' in img.info:
                del img.info['exif']
            
            # Garante que a imagem esteja no modo de cor correto para salvar em WebP
            if img.mode not in ['RGB', 'RGBA']:
                img = img.convert('RGB')
            
            buffer = BytesIO()
            
            if lossless:
                # Salva em modo WebP sem perdas de qualidade
                save_params = {'format': 'WEBP', 'lossless': True, 'method': 6} # method=6 é mais lento, mas comprime melhor
            else:
                # Salva em modo WebP com compressão (qualidade ajustável)
                save_params = {'format': 'WEBP', 'quality': quality}
            
            img.save(buffer, **save_params)
            
            new_filename = f"{os.path.splitext(filename)[0]}.webp"
            
            return new_filename, buffer.getvalue()
    except Exception as e:
        logger.error(f"Falha ao processar imagem em memória '{filename}': {e}", exc_info=True)
        return filename, file_bytes