import asyncio
import base64
import io
import cv2
import langid
import requests
import os
import re
import torch
import time
import logging
import numpy as np
from PIL import Image
from typing import List
from aiohttp import web
from marshmallow import Schema, fields, ValidationError

from .args import DEFAULT_ARGS
from .utils import (
    BASE_PATH,
    LANGAUGE_ORIENTATION_PRESETS,
    TextBlock,
    ModelWrapper,
    Context,
    load_image,
    dump_image,
    replace_prefix,
    visualize_textblocks,
    add_file_logger,
    remove_file_logger,
    count_valuable_text,
    rgb2hex,
    get_color_name,
    is_url,
)

from .detection import DETECTORS, dispatch as dispatch_detection, prepare as prepare_detection
from .upscaling import dispatch as dispatch_upscaling, prepare as prepare_upscaling
from .ocr import OCRS, dispatch as dispatch_ocr, prepare as prepare_ocr
from .mask_refinement import dispatch as dispatch_mask_refinement
from .inpainting import INPAINTERS, dispatch as dispatch_inpainting, prepare as prepare_inpainting
from .translators import (
    TRANSLATORS,
    VALID_LANGUAGES,
    LanguageUnsupportedException,
    TranslatorChain,
    dispatch as dispatch_translation,
    prepare as prepare_translation,
)
from .text_rendering import dispatch as dispatch_rendering, dispatch_eng_render


# Will be overwritten by __main__.py if module is being run directly (with python -m)
logger = logging.getLogger('manga_translator')

def set_main_logger(l):
    global logger
    logger = l

class TranslationInterrupt(Exception):
    """
    Can be raised from within a progress hook to prematurely terminate
    the translation
    """
    pass

class MangaTranslator():

    def __init__(self, params: dict = None):
        self._progress_hooks = []
        self._add_logger_hook()

        params = params or {}
        self.verbose = params.get('verbose', False)
        self.ignore_errors = params.get('ignore_errors', False)

        self.device = 'cuda' if params.get('use_cuda', False) else 'cpu'
        self._cuda_limited_memory = params.get('use_cuda_limited', False)
        if self._cuda_limited_memory and not self.using_cuda:
            self.device = 'cuda'
        if self.using_cuda and not torch.cuda.is_available():
            raise Exception('CUDA compatible device could not be found whilst --use-cuda args was set...')

        self.result_sub_folder = ''

    @property
    def using_cuda(self):
        return self.device.startswith('cuda')

    async def translate_path(self, path: str, dest: str = None, params: dict = None):
        """
        Translates an image or folder (recursively) specified through the path.
        """
        path = os.path.expanduser(path)
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        path = os.path.abspath(path)
        dest = os.path.abspath(os.path.expanduser(dest)) if dest else ''
        params = params or {}

        # TODO: accept * in file paths

        if os.path.isfile(path):
            # Determine destination file path
            if not dest:
                # Use the same folder as the source
                p, ext = os.path.splitext(path)
                dest = f'{p}-translated.png'
            elif not os.path.basename(dest):
                p, ext = os.path.splitext(os.path.basename(path))
                # If the folders differ use the original filename from the source
                if os.path.dirname(path) != dest:
                    dest = os.path.join(dest, f'{p}.png')
                else:
                    dest = os.path.join(dest, f'{p}-translated.png')
            dest_root = os.path.dirname(dest)
            await self._translate_file(path, dest, params)

        elif os.path.isdir(path):
            # Determine destination folder path
            if path[-1] == '\\' or path[-1] == '/':
                path = path[:-1]
            dest = dest or path + '-translated'
            if os.path.exists(dest) and not os.path.isdir(dest):
                raise FileExistsError(dest)

            translated_count = 0
            for root, subdirs, files in os.walk(path):
                files.sort()
                dest_root = replace_prefix(root, path, dest)
                os.makedirs(dest_root, exist_ok=True)
                for f in files:
                    if f.lower() == '.thumb':
                        continue
                    file_path = os.path.join(root, f)
                    output_dest = replace_prefix(file_path, path, dest)
                    if os.path.exists(output_dest):
                        continue
                    logger.info(f'Processing {file_path} -> {output_dest}')
                    await self._translate_file(file_path, output_dest, params)
                    translated_count += 1
            if translated_count == 0:
                logger.info(f'No untranslated files found')
            else:
                logger.info(f'Done. Translated {translated_count} image{"" if translated_count == 1 else "s"}')

    async def _translate_file(self, path: str, dest: str, params: dict):
        try:
            img = Image.open(path)
        except Exception:
            return

        translation_dict = await self.translate(img, params)

        result = None
        if translation_dict.result is not None:
            # Translation got saved into result
            result = translation_dict.result
        elif translation_dict.text_regions is not None:
            # No text regions with text found
            result = img
        if result:
            result.save(dest)
            await self._report_progress('saved', True)

        save_text_to_file = translation_dict.save_text or translation_dict.save_text_file or translation_dict.prep_manual
        if save_text_to_file:
            if translation_dict.prep_manual:
                # Save original image next to translated
                p, ext = os.path.splitext(dest)
                img_filename = p + '-orig' + ext
                img_path = os.path.join(os.path.dirname(dest), img_filename)
                img.save(img_path)
            if translation_dict.text_regions:
                self.save_text_to_file(dest, translation_dict)

    async def translate(self, image: Image.Image, params: dict = None) -> Context:
        """
        Translates a PIL image from a manga. Returns dict with result and intermediates of translation.

        ```py
        translation_dict = await translator.translate(image)
        result = translation_dict.result
        ```
        """
        # TODO: Take list of images to speed up batch processing

        # Turn dict to context to make values also accessible through params.<property>
        params = params or {}
        ctx = Context(**params)
        self._preprocess_params(ctx)

        if ctx.chatgpt_prompt_file:
            from .translators import chatgpt
            with open(ctx.chatgpt_prompt_file, 'r') as f:
                chatgpt.PROMPT_OVERWRITE = f.read()
        if ctx.chatgpt_temperature:
            from .translators import chatgpt
            chatgpt.TEMPERATURE_OVERWRITE = ctx.chatgpt_temperature
        if ctx.model_dir:
            ModelWrapper._MODEL_DIR = ctx.model_dir

        ctx.input = image
        ctx.result = None

        attempts = 0
        while ctx.retries == -1 or attempts < ctx.retries + 1:
            if attempts > 0:
                logger.info(f'Retrying translation! Attempt {attempts}'
                            + (f' of {ctx.retries}' if ctx.retries != -1 else ''))
            try:
                # preload and download models (not strictly necessary, remove to lazy load)
                logger.info('Loading models')
                if ctx.upscale_ratio:
                    await prepare_upscaling(ctx.upscaler)
                await prepare_detection(ctx.detector)
                await prepare_ocr(ctx.ocr, self.device)
                await prepare_inpainting(ctx.inpainter, self.device)
                await prepare_translation(ctx.translator)

                # translate
                return await self._translate(ctx)
            except TranslationInterrupt:
                break
            except Exception as e:
                if isinstance(e, LanguageUnsupportedException):
                    await self._report_progress('error-lang', True)
                else:
                    await self._report_progress('error', True)
                if not self.ignore_errors and not (ctx.retries == -1 or attempts < ctx.retries):
                    raise
                else:
                    logger.error(f'{e.__class__.__name__}: {e}',
                                 exc_info=e if self.verbose else None)
            attempts += 1
        return ctx

    def _preprocess_params(self, ctx: Context):
        # params auto completion
        for arg in DEFAULT_ARGS:
            ctx.setdefault(arg, DEFAULT_ARGS[arg])

        if 'direction' not in ctx:
            if ctx.force_horizontal:
                ctx.direction = 'h'
            elif ctx.force_vertical:
                ctx.direction = 'v'
            else:
                ctx.direction = 'auto'
        if 'alignment' not in ctx:
            if ctx.align_left:
                ctx.alignment = 'left'
            elif ctx.align_center:
                ctx.alignment = 'center'
            elif ctx.align_right:
                ctx.alignment = 'right'
            else:
                ctx.alignment = 'auto'
        if ctx.prep_manual:
            ctx.renderer = 'none'
        ctx.setdefault('renderer', 'manga2eng' if ctx.manga2eng else 'default')

        if ctx.selective_translation is not None:
            ctx.selective_translation.target_lang = ctx.target_lang
            ctx.translator = ctx.selective_translation
        elif ctx.translator_chain is not None:
            ctx.target_lang = ctx.translator_chain.langs[-1]
            ctx.translator = ctx.translator_chain
        else:
            ctx.translator = TranslatorChain(f'{ctx.translator}:{ctx.target_lang}')

        if ctx.filter_text:
            ctx.filter_text = re.compile(ctx.filter_text)
        if ctx.filter_trans:
            ctx.filter_trans = re.compile(ctx.filter_trans)

    async def _translate(self, ctx: Context) -> Context:

        # The default text detector doesn't work very well on smaller images, might want to
        # consider adding automatic upscaling on certain kinds of small images.
        if ctx.upscale_ratio:
            await self._report_progress('upscaling')
            ctx.upscaled = await self._run_upscaling(ctx)
        else:
            ctx.upscaled = ctx.input

        ctx.img_rgb, ctx.img_alpha = load_image(ctx.upscaled)

        await self._report_progress('detection')
        ctx.text_regions, ctx.mask_raw, ctx.mask = await self._run_detection(ctx)
        if self.verbose:
            cv2.imwrite(self._result_path('mask_raw.png'), ctx.mask_raw)
            bboxes = visualize_textblocks(cv2.cvtColor(ctx.img_rgb, cv2.COLOR_BGR2RGB), ctx.text_regions)
            cv2.imwrite(self._result_path('bboxes.png'), bboxes)

        if not ctx.text_regions:
            await self._report_progress('skip-no-regions', True)
            return ctx

        await self._report_progress('ocr')
        ctx.text_regions = await self._run_ocr(ctx)
        if not ctx.text_regions:
            await self._report_progress('skip-no-text', True)
            return ctx

        await self._report_progress('translating')
        ctx.text_regions = await self._run_text_translation(ctx)

        if not ctx.text_regions:
            await self._report_progress('error-translating', True)
            return ctx

        # Delayed mask refinement to take advantage of the region filtering done by ocr
        if ctx.mask is None:
            await self._report_progress('mask-generation')
            ctx.mask = await self._run_mask_refinement(ctx)

        if self.verbose:
            inpaint_input_img = await dispatch_inpainting('none', ctx.img_rgb, ctx.mask, ctx.inpainting_size, self.using_cuda, self.verbose)
            cv2.imwrite(self._result_path('inpaint_input.png'), cv2.cvtColor(inpaint_input_img, cv2.COLOR_RGB2BGR))
            cv2.imwrite(self._result_path('mask_final.png'), ctx.mask)

        await self._report_progress('inpainting')
        ctx.img_inpainted = await self._run_inpainting(ctx)

        if self.verbose:
            cv2.imwrite(self._result_path('inpainted.png'), cv2.cvtColor(ctx.img_inpainted, cv2.COLOR_RGB2BGR))

        await self._report_progress('rendering')
        ctx.img_rendered = await self._run_text_rendering(ctx)

        await self._report_progress('finished', True)
        ctx.result = dump_image(ctx.img_rendered, ctx.img_alpha)

        if ctx.revert_upscaling:
            await self._report_progress('downscaling')
            ctx.result = ctx.result.resize(ctx.input.size)

        return ctx

    def _result_path(self, path: str) -> str:
        return os.path.join(BASE_PATH, 'result', self.result_sub_folder, path)

    def add_progress_hook(self, ph):
        self._progress_hooks.append(ph)

    async def _report_progress(self, state: str, finished: bool = False):
        for ph in self._progress_hooks:
            await ph(state, finished)

    def _add_logger_hook(self):
        LOG_MESSAGES = {
            'upscaling':            'Running upscaling',
            'detection':            'Running text detection',
            'ocr':                  'Running ocr',
            'mask-generation':      'Running mask refinement',
            'translating':          'Running text translation',
            'rendering':            'Running rendering',
            'downscaling':          'Running downscaling',
            'saved':                'Saving results', # TODO: Pass ctx to logger hook and add save destination
        }
        LOG_MESSAGES_SKIP = {
            'skip-no-regions':      'No text regions! - Skipping',
            'skip-no-text':         'No text regions with text! - Skipping',
        }
        LOG_MESSAGES_ERROR = {
            'error-translating':    'Text translator returned empty queries',
            # 'error-lang':           'Target language not supported by chosen translator',
        }

        async def ph(state, finished):
            if state in LOG_MESSAGES:
                logger.info(LOG_MESSAGES[state])
            elif state in LOG_MESSAGES_SKIP:
                logger.warn(LOG_MESSAGES_SKIP[state])
            elif state in LOG_MESSAGES_ERROR:
                logger.error(LOG_MESSAGES_ERROR[state])

        self.add_progress_hook(ph)

    def save_text_to_file(self, image_path: str, ctx: Context):
        cached_colors = []

        def identify_colors(fg_rgb: List[int]):
            idx = 0
            for rgb, _ in cached_colors:
                # If similar color already saved
                if abs(rgb[0] - fg_rgb[0]) + abs(rgb[1] - fg_rgb[1]) + abs(rgb[2] - fg_rgb[2]) < 50:
                    break
                else:
                    idx += 1
            else:
                cached_colors.append((fg_rgb, get_color_name(fg_rgb)))
            return idx + 1, cached_colors[idx][1]

        s = f'\n[{image_path}]\n'
        for i, region in enumerate(ctx.text_regions):
            fore, back = region.get_font_colors()
            color_id, color_name = identify_colors(fore)

            s += f'\n-- {i+1} --\n'
            s += f'color: #{color_id}: {color_name} (fg, bg: {rgb2hex(*fore)} {rgb2hex(*back)})\n'
            s += f'text:  {region.get_text()}\n'
            s += f'trans: {region.translation}'
        s += '\n\n'

        text_output_file = ctx.text_output_file
        if not text_output_file:
            text_output_file = os.path.join(os.path.dirname(image_path), '_translations.txt')

        with open(text_output_file, 'a', encoding='utf-8') as f:
            f.write(s)

    async def _run_upscaling(self, ctx: Context):
        return (await dispatch_upscaling(ctx.upscaler, [ctx.input], ctx.upscale_ratio, self.device))[0]

    async def _run_detection(self, ctx: Context):
        return await dispatch_detection(ctx.detector, ctx.img_rgb, ctx.detection_size, ctx.text_threshold, ctx.box_threshold,
                                        ctx.unclip_ratio, ctx.det_invert, ctx.det_gamma_correct, ctx.det_rotate, ctx.det_auto_rotate,
                                        self.device, self.verbose)

    async def _run_ocr(self, ctx: Context):
        text_regions = await dispatch_ocr(ctx.ocr, ctx.img_rgb, ctx.text_regions, self.device, self.verbose)
        # Filter out regions by original text
        new_text_regions = []
        for region in text_regions:
            text = region.get_text()
            if text.isnumeric() \
                or (ctx.filter_text and re.search(ctx.filter_text, text)) \
                or count_valuable_text(text) <= 1 \
                or is_url(text):
                if text.strip():
                    logger.info(f'Filtered out: {text}')
            else:
                new_text_regions.append(region)
        return new_text_regions

    async def _run_text_translation(self, ctx: Context):
        translated_sentences = await dispatch_translation(ctx.translator, [region.get_text() for region in ctx.text_regions], ctx.use_mtpe,
                                                          'cpu' if self._cuda_limited_memory else self.device)

        for region, translation in zip(ctx.text_regions, translated_sentences):
            if ctx.uppercase:
                translation = translation.upper()
            elif ctx.lowercase:
                translation = translation.upper()
            region.translation = translation
            region.target_lang = ctx.target_lang
            region._alignment = ctx.alignment
            region._direction = ctx.direction

        # Filter out regions by their translations
        new_text_regions = []
        for region in ctx.text_regions:
            if region.translation.isnumeric() \
                or (ctx.filter_trans and re.search(ctx.filter_trans, region.translation)) \
                or count_valuable_text(region.translation) <= 1:
                if region.translation.strip():
                    logger.info(f'Filtered out: {region.translation}')
            else:
                new_text_regions.append(region)
        return new_text_regions

    async def _run_mask_refinement(self, ctx: Context):
        return await dispatch_mask_refinement(ctx.text_regions, ctx.img_rgb, ctx.mask_raw, 'fit_text', self.verbose)

    async def _run_inpainting(self, ctx: Context):
        return await dispatch_inpainting(ctx.inpainter, ctx.img_rgb, ctx.mask, ctx.inpainting_size, self.using_cuda, self.verbose)

    async def _run_text_rendering(self, ctx: Context):
        if ctx.renderer == 'none':
            output = ctx.img_inpainted
        # manga2eng currently only supports horizontal rendering
        elif ctx.renderer == 'manga2eng' and ctx.text_regions and LANGAUGE_ORIENTATION_PRESETS.get(ctx.text_regions[0].target_lang) == 'h':
            output = await dispatch_eng_render(ctx.img_inpainted, ctx.img_rgb, ctx.text_regions, ctx.font_path)
        else:
            output = await dispatch_rendering(ctx.img_inpainted, ctx.text_regions, ctx.font_path, ctx.font_size, ctx.font_size_offset,
                                              ctx.font_size_minimum, ctx.render_mask)
        return output


class MangaTranslatorWeb(MangaTranslator):
    """
    Translator client that executes tasks on behalf of the webserver in web_main.py.
    """
    def __init__(self, params: dict = None):
        super().__init__(params)
        self.host = params.get('host', '127.0.0.1')
        if self.host == '0.0.0.0':
            self.host = '127.0.0.1'
        self.port = str(params.get('port', 5003))
        self.nonce = params.get('nonce', '')
        self.ignore_errors = params.get('ignore_errors', True)
        self._task_id = None
        self._params = None

    async def listen(self, translation_params: dict = None):
        """
        Listens for translation tasks from web server.
        """
        logger.info('Waiting for translation tasks')

        async def sync_state(state: str, finished: bool):
            # wait for translation to be saved first (bad solution?)
            finished = finished and not state == 'finished'
            while True:
                try:
                    data = {
                        'task_id': self._task_id,
                        'nonce': self.nonce,
                        'state': state,
                        'finished': finished,
                    }
                    requests.post(f'http://{self.host}:{self.port}/task-update-internal', json=data, timeout=20)
                    break
                except Exception:
                    # if translation is finished server has to know
                    if finished:
                        continue
                    else:
                        break
        self.add_progress_hook(sync_state)

        while True:
            self._task_id, self._params = self._get_task()
            if self._params and 'exit' in self._params:
                break
            if not (self._task_id and self._params):
                await asyncio.sleep(0.1)
                continue

            self.result_sub_folder = self._task_id
            logger.info(f'Processing task {self._task_id}')
            if translation_params is not None:
                # Combine default params with params chosen by webserver
                for p, default_value in translation_params.items():
                    current_value = self._params.get(p)
                    self._params[p] = current_value if current_value is not None else default_value
            if self.verbose:
                # Write log file
                log_file = self._result_path('log.txt')
                add_file_logger(log_file)

            await self.translate_path(self._result_path('input.png'), self._result_path('final.png'), params=self._params)
            print()

            if self.verbose:
                remove_file_logger(log_file)
            self._task_id = None
            self._params = None
            self.result_sub_folder = ''

    def _get_task(self):
        try:
            rjson = requests.get(f'http://{self.host}:{self.port}/task-internal?nonce={self.nonce}', timeout=3600).json()
            return rjson.get('task_id'), rjson.get('data')
        except Exception:
            return None, None

    async def _run_ocr(self, ctx: Context):
        regions = await super()._run_ocr(ctx)
        if ctx.manual:
            requests.post(f'http://{self.host}:{self.port}/request-translation-internal', json={
                'task_id': self._task_id,
                'nonce': self.nonce,
                'texts': [r.get_text() for r in regions],
            }, timeout=20)
        return regions

    async def _run_text_translation(self, ctx: Context):
        if ctx.get('manual', False):
            requests.post(f'http://{self.host}:{self.port}/request-translation-internal', json={
                'task_id': self._task_id,
                'nonce': self.nonce,
                'texts': [r.get_text() for r in ctx.text_regions]
            }, timeout=20)

            # wait for at most 1 hour for manual translation
            wait_until = time.time() + 3600
            while time.time() < wait_until:
                ret = requests.post(f'http://{self.host}:{self.port}/get-translation-result-internal', json={
                    'task_id': self._task_id,
                    'nonce': self.nonce
                }, timeout=20).json()
                if 'result' in ret:
                    translated = ret['result']
                    if isinstance(translated, str):
                        if translated == 'error':
                            return None
                    for blk, tr in zip(ctx.text_regions, translated):
                        blk.translation = tr
                        blk.target_lang = ctx.translator.langs[-1]
                    return translated
                await asyncio.sleep(0.1)
        else:
            return await super()._run_text_translation(ctx)


class MangaTranslatorWS(MangaTranslator):

    def __init__(self, params: dict = None):
        super().__init__(params)
        self.url = params.get('ws_url')
        self.secret = params.get('ws_secret', os.getenv('WS_SECRET', ''))
        self.ignore_errors = params.get('ignore_errors', True)
        self._task_id = None

    async def listen(self, translation_params: dict = None):
        import io
        import shutil
        import websockets

        from .server import ws_pb2

        async def sync_state(state, finished):
            msg = ws_pb2.WebSocketMessage()
            msg.status.id = self._task_id
            msg.status.status = state
            await websocket.send(msg.SerializeToString())

        self.add_progress_hook(sync_state)

        async for websocket in websockets.connect(self.url, extra_headers={'x-secret': self.secret}, max_size=100_000_000):
            try:
                logger.info('Connected to websocket server')

                async for raw in websocket:
                    msg = ws_pb2.WebSocketMessage()
                    msg.ParseFromString(raw)
                    if msg.WhichOneof('message') == 'new_task':
                        task = msg.new_task
                        self._task_id = task.id

                        if self.verbose:
                            shutil.rmtree(f'result/{self._task_id}', ignore_errors=True)
                            os.makedirs(f'result/{self._task_id}', exist_ok=True)

                        params = {
                            'target_language': task.target_language,
                            'detector': task.detector,
                            'direction': task.direction,
                            'translator': task.translator,
                            'size': task.size,
                        }

                        logger.info(f'-- Processing task {self._task_id}')
                        if translation_params:
                            for p, default_value in translation_params.items():
                                current_value = params.get(p)
                                params[p] = current_value if current_value is not None else default_value
                        image = Image.open(io.BytesIO(task.source_image))
                        translation_dict = await self.translate(image, params)
                        output = translation_dict.result
                        if output is None:
                            output = Image.fromarray(np.zeros((output.height, output.width, 4), dtype=np.uint8))

                        img = io.BytesIO()
                        output.save(img, format='PNG')
                        if self.verbose:
                            output.save(self._result_path('ws_final.png'))

                        img_bytes = img.getvalue()

                        result = ws_pb2.WebSocketMessage()
                        result.finish_task.id = self._task_id
                        result.finish_task.translation_mask = img_bytes
                        await websocket.send(result.SerializeToString())

                        logger.info('Waiting for translation tasks')
                        self._task_id = None

            except Exception as e:
                logger.error(f'{e.__class__.__name__}: {e}', exc_info=e if self.verbose else None)

    async def _run_text_rendering(self, ctx: Context):
        render_mask = np.copy(ctx.mask)
        render_mask[render_mask < 127] = 0
        render_mask[render_mask >= 127] = 1
        render_mask = render_mask[:, :, None]

        output = await super()._run_text_rendering(ctx)
        render_mask[np.sum(ctx.img_rgb != output, axis=2) > 0] = 1
        ctx.render_mask = render_mask
        if self.verbose:
            cv2.imwrite(self._result_path('ws_render_in.png'), cv2.cvtColor(ctx.img_rgb, cv2.COLOR_RGB2BGR))
            cv2.imwrite(self._result_path('ws_render_out.png'), cv2.cvtColor(output, cv2.COLOR_RGB2BGR))
            cv2.imwrite(self._result_path('ws_mask.png'), render_mask * 255)

        # only keep sections in mask
        if self.verbose:
            cv2.imwrite(self._result_path('ws_inmask.png'), cv2.cvtColor(ctx.img_rgb, cv2.COLOR_RGB2BGRA) * render_mask)
        output = cv2.cvtColor(output, cv2.COLOR_RGB2RGBA) * render_mask
        if self.verbose:
            cv2.imwrite(self._result_path('ws_output.png'), cv2.cvtColor(output, cv2.COLOR_RGBA2BGRA) * render_mask)

        return output

class MangaTranslatorAPI(MangaTranslator):
    def __init__(self, params: dict = None):
        import nest_asyncio
        nest_asyncio.apply()
        super().__init__(params)
        self.host = params.get('host', '127.0.0.1')
        self.port = params.get('port', '5003')
        self.log_web = params.get('log_web', False)
        self.ignore_errors = params.get('ignore_errors', True)
        self._task_id = None
        self._params = None
        self.params = params

    async def get_file(self, image, base64Images, url) -> Image:
        if image is not None:
            content = image.file.read()
        elif base64Images is not None:
            base64Images = base64Images
            if base64Images.__contains__('base64,'):
                base64Images = base64Images.split('base64,')[1]
            content = base64.b64decode(base64Images)
        elif url is not None:
            from aiohttp import ClientSession
            async with ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        content = await resp.read()
                    else:
                        return web.json_response({'status': 'error'})
        else:
            raise ValidationError("donest exist")
        img = Image.open(io.BytesIO(content))

        img.verify()
        img = Image.open(io.BytesIO(content))
        if img.width * img.height > 8000**2:
            raise ValidationError("to large")
        return img

    async def listen(self, translation_params: dict = None):
        self.params = translation_params
        app = web.Application(client_max_size=1024 * 1024 * 50)
        routes = web.RouteTableDef()
        run_until_state = ''

        def ph(state, finished):
            if run_until_state and run_until_state == state and not finished:
                raise TranslationInterrupt()

        @routes.post("/get_text")
        async def text_api(req):
            run_until_state = 'ocr'
            return await self.err_handling(self.texts_exec, req, self.format_translate)

        @routes.post("/translate")
        async def translate_api(req):
            run_until_state = 'translating'
            return await self.err_handling(self.translate_exec, req, self.format_translate)

        @routes.post("/inpaint_translate")
        async def inpaint_translate_api(req):
            run_until_state = 'inpainting'
            return await self.err_handling(self.inpaint_translate_exec, req, self.format_translate)

        #@routes.post("/file")
        async def file_api(req):
            #TODO: return file
            return await self.err_handling(self.file_exec, req, None)

        app.add_routes(routes)
        web.run_app(app, host=self.host, port=self.port)

    async def texts_exec(self, translation_params, img):
        return await self.translate(img, translation_params)

    async def translate_exec(self, translation_params, img):
        return await self.translate(img, translation_params)

    async def inpaint_translate_exec(self, translation_params, img):
        return await self.translate(img, translation_params)

    async def file_exec(self, translation_params, img):
        return await self.translate(img, translation_params)

    async def err_handling(self, func, req, format):
        try:
            if req.content_type == 'application/json' or req.content_type == 'multipart/form-data':
                if req.content_type == 'application/json':
                    d = await req.json()
                else:
                    d = await req.post()
                schema = self.PostSchema()
                data = schema.load(d)
                if data.get('image') is None and data.get('base64Images') is None and data.get('url') is None:
                    return web.json_response({'error': "Missing input", 'status': 422})
                fil = await self.get_file(data.get('image'), data.get('base64Images'), data.get('url'))
                if 'image' in data:
                    del data['image']
                if 'base64Images' in data:
                    del data['base64Images']
                if 'url' in data:
                    del data['url']
                loaded_data = await func(dict(self.params, **data), fil)
                return format(loaded_data)
            else:
                return web.json_response({'error': "Wrong content type: " + req.content_type, 'status': 415},
                                         status=415)
        except ValueError:
            return web.json_response({'error': "Wrong input type", 'status': 422}, status=422)

        except ValidationError as e:
            print(e)
            return web.json_response({'error': "Input invalid", 'status': 422}, status=422)


    def format_translate(self, ctx: Context):
        text_regions = ctx.text_regions
        inpaint = ctx.img_inpainted
        results = []
        for i, blk in enumerate(text_regions):
            minX, minY, maxX, maxY = blk.xyxy
            text = text_regions[i].get_text()
            trans = text_regions[i].translation
            overlay = inpaint[minY:maxY, minX:maxX]
            retval, buffer = cv2.imencode('.jpg', overlay)
            jpg_as_text = base64.b64encode(buffer)
            color1, color2 = text_regions[i].get_font_colors()
            background = jpg_as_text.decode("utf-8")
            results.append({
                'originalText': text,
                'minX': int(minX),
                'minY': int(minY),
                'maxX': int(maxX),
                'maxY': int(maxY),
                'language': langid.classify(text)[0],
                'translatedText': trans,
                'textColor': {
                    'fg': color1.tolist(),
                    'bg': color2.tolist()
                },
                'background': "data:image/jpg;base64," + background
            })
        return web.json_response({'images': [results]})

    class PostSchema(Schema):
        size = fields.Str(required=False, validate=lambda a: a.upper() not in ['S', 'M', 'L', 'X'])
        translator = fields.Str(required=False,
                                validate=lambda a: a.lower() not in TRANSLATORS)
        target_language = fields.Str(required=False,
                                     validate=lambda a: a.upper() not in VALID_LANGUAGES)
        detector = fields.Str(required=False, validate=lambda a: a.lower() not in DETECTORS)
        direction = fields.Str(required=False,
                               validate=lambda a: a.lower() not in set(['auto', 'h', 'v']))
        inpainter = fields.Str(required=False,
                               validate=lambda a: a.lower() not in INPAINTERS)
        ocr = fields.Str(required=False, validate=lambda a: a.lower() not in OCRS)
        upscale_ratio = fields.Integer(required=False)
        text_threshold = fields.Float(required=False)
        box_threshold = fields.Float(required=False)
        unclip_ratio = fields.Float(required=False)
        inpainting_size = fields.Integer(required=False)
        font_size_offset = fields.Integer(required=False)
        text_mag_ratio = fields.Integer(required=False)
        det_rearrange_max_batches = fields.Integer(required=False)
        manga2eng = fields.Boolean(required=False)
        base64Images = fields.Raw(required=False)
        image = fields.Raw(required=False)
        url = fields.Raw(required=False)
        fingerprint = fields.Raw(required=False)
        clientUuid = fields.Raw(required=False)
