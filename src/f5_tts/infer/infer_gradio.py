# ruff: noqa: E402
# Above allows ruff to ignore E402: module level import not at top of file

import json
import re
import tempfile
import os
from collections import OrderedDict
from importlib.resources import files

import click
import gradio as gr
import numpy as np
import soundfile as sf
import torchaudio
from cached_path import cached_path
from transformers import AutoModelForCausalLM, AutoTokenizer

try:
    import spaces

    USING_SPACES = True
except ImportError:
    USING_SPACES = False


def gpu_decorator(func):
    if USING_SPACES:
        return spaces.GPU(func)
    else:
        return func


from f5_tts.model import DiT, UNetT
from f5_tts.infer.utils_infer import (
    load_vocoder,
    load_model,
    preprocess_ref_audio_text,
    infer_process,
    remove_silence_for_generated_wav,
    save_spectrogram,
)


DEFAULT_TTS_MODEL = "F5-TTS"
tts_model_choice = DEFAULT_TTS_MODEL

DEFAULT_TTS_MODEL_CFG = [
    "hf://SWivid/F5-TTS/F5TTS_Base/model_1200000.safetensors",
    "hf://SWivid/F5-TTS/F5TTS_Base/vocab.txt",
    json.dumps(dict(dim=1024, depth=22, heads=16, ff_mult=2, text_dim=512, conv_layers=4)),
]


# load models

vocoder = load_vocoder()


def load_f5tts(ckpt_path=str(cached_path("hf://SWivid/F5-TTS/F5TTS_Base/model_1200000.safetensors"))):
    F5TTS_model_cfg = dict(dim=1024, depth=22, heads=16, ff_mult=2, text_dim=512, conv_layers=4)
    return load_model(DiT, F5TTS_model_cfg, ckpt_path)


def load_e2tts(ckpt_path=str(cached_path("hf://SWivid/E2-TTS/E2TTS_Base/model_1200000.safetensors"))):
    E2TTS_model_cfg = dict(dim=1024, depth=24, heads=16, ff_mult=4)
    return load_model(UNetT, E2TTS_model_cfg, ckpt_path)


def load_custom(ckpt_path: str, vocab_path="", model_cfg=None):
    ckpt_path, vocab_path = ckpt_path.strip(), vocab_path.strip()
    if (ckpt_path.startswith("hf://")):
        ckpt_path = str(cached_path(ckpt_path))
    if (vocab_path.startswith("hf://")):
        vocab_path = str(cached_path(vocab_path))
    if (model_cfg is None):
        model_cfg = dict(dim=1024, depth=22, heads=16, ff_mult=2, text_dim=512, conv_layers=4)
    return load_model(DiT, model_cfg, ckpt_path, vocab_file=vocab_path)


F5TTS_ema_model = load_f5tts()
E2TTS_ema_model = load_e2tts() if USING_SPACES else None
custom_ema_model, pre_custom_path = None, ""

chat_model_state = None
chat_tokenizer_state = None


@gpu_decorator
def generate_response(messages, model, tokenizer):
    """Generate response using Qwen"""
    text = tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True,
    )

    model_inputs = tokenizer([text], return_tensors="pt").to(model.device)
    generated_ids = model.generate(
        **model_inputs,
        max_new_tokens=512,
        temperature=0.7,
        top_p=0.95,
    )

    generated_ids = [
        output_ids[len(input_ids) :] for input_ids, output_ids in zip(model_inputs.input_ids, generated_ids)
    ]
    return tokenizer.batch_decode(generated_ids, skip_special_tokens=True)[0]


@gpu_decorator
def infer(
    ref_audio_orig,
    ref_text,
    gen_text,
    model,
    remove_silence,
    cross_fade_duration=0.15,
    nfe_step=32,
    speed=1,
    show_info=gr.Info,
):
    if not ref_audio_orig:
        gr.Warning("Please provide reference audio.")
        return gr.update(), gr.update(), ref_text

    if not gen_text.strip():
        gr.Warning("Please enter text to generate.")
        return gr.update(), gr.update(), ref_text

    ref_audio, ref_text = preprocess_ref_audio_text(ref_audio_orig, ref_text, show_info=show_info)

    if model == "F5-TTS":
        ema_model = F5TTS_ema_model
    elif model == "E2-TTS":
        global E2TTS_ema_model
        if E2TTS_ema_model is None:
            show_info("Loading E2-TTS model...")
            E2TTS_ema_model = load_e2tts()
        ema_model = E2TTS_ema_model
    elif isinstance(model, list) and model[0] == "Custom":
        assert not USING_SPACES, "Only official checkpoints allowed in Spaces."
        global custom_ema_model, pre_custom_path
        if pre_custom_path != model[1]:
            show_info("Loading Custom TTS model...")
            custom_ema_model = load_custom(model[1], vocab_path=model[2], model_cfg=model[3])
            pre_custom_path = model[1]
        ema_model = custom_ema_model

    final_wave, final_sample_rate, combined_spectrogram = infer_process(
        ref_audio,
        ref_text,
        gen_text,
        ema_model,
        vocoder,
        cross_fade_duration=cross_fade_duration,
        nfe_step=nfe_step,
        speed=speed,
        show_info=show_info,
        progress=gr.Progress(),
    )

    # Remove silence
    if remove_silence:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as f:
            sf.write(f.name, final_wave, final_sample_rate)
            remove_silence_for_generated_wav(f.name)
            final_wave, _ = torchaudio.load(f.name)
        final_wave = final_wave.squeeze().cpu().numpy()

    # Save the spectrogram
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_spectrogram:
        spectrogram_path = tmp_spectrogram.name
        save_spectrogram(combined_spectrogram, spectrogram_path)

    return (final_sample_rate, final_wave), spectrogram_path, ref_text


with gr.Blocks() as app_credits:
    gr.Markdown("""
# Credits

* [mrfakename](https://github.com/fakerybakery) for the original [online demo](https://huggingface.co/spaces/mrfakename/E2-F5-TTS)
* [RootingInLoad](https://github.com/RootingInLoad) for initial chunk generation and podcast app exploration
* [jpgallegoar](https://github.com/jpgallegoar) for multiple speech-type generation & voice chat
""")

# Chapter state data structure
class ChapterState:
    def __init__(self):
        # Increase to 7 chapters
        self.chapters = [
            {"title": f"Chapter {i+1}", "content": "", "audio": None, "spectrogram": None, "generated": False}
            for i in range(7)
        ]
        self.current_chapter = 0
        
    def get_current_chapter(self):
        return self.chapters[self.current_chapter]
    
    def get_chapter(self, index):
        return self.chapters[index]
    
    def set_current_chapter_content(self, content):
        self.chapters[self.current_chapter]["content"] = content
        
    def set_chapter_content(self, index, content):
        self.chapters[index]["content"] = content
        
    def set_current_chapter_audio(self, audio, spectrogram):
        self.chapters[self.current_chapter]["audio"] = audio
        self.chapters[self.current_chapter]["spectrogram"] = spectrogram
        self.chapters[self.current_chapter]["generated"] = True
        
    def set_chapter_audio(self, index, audio, spectrogram):
        self.chapters[index]["audio"] = audio
        self.chapters[index]["spectrogram"] = spectrogram
        self.chapters[index]["generated"] = True
        
    def delete_chapter_audio(self, index):
        """Delete the audio for a specific chapter"""
        self.chapters[index]["audio"] = None
        self.chapters[index]["spectrogram"] = None
        self.chapters[index]["generated"] = False

# Create chapter state instance
chapter_state = ChapterState()

with gr.Blocks() as app_tts:
    gr.Markdown("# Batched TTS")
    
    with gr.Row():
        ref_audio_input = gr.Audio(label="Reference Audio", type="filepath")
        ref_text_input = gr.Textbox(
            label="Reference Text",
            info="Leave blank to automatically transcribe the reference audio. If you enter text it will override automatic transcription.",
            lines=2,
        )
    
    # Chapter navigation and generation
    with gr.Row():
        with gr.Column(scale=1):
            gr.Markdown("## Chapter Navigation")
            chapter_buttons = []
            chapter_generate_buttons = []
            chapter_delete_buttons = []
            
            # Create buttons for each chapter with their own generate and delete buttons
            for i in range(7):
                # Chapter button in its own row
                with gr.Row():
                    btn = gr.Button(f"Chapter {i+1}", variant="secondary", scale=2)
                    chapter_buttons.append(btn)
                
                # Generate and Delete buttons exactly side by side in one row
                with gr.Row():
                    gen_btn = gr.Button(f"Generate Ch.{i+1}", variant="primary", scale=1)
                    del_btn = gr.Button(f"Delete Ch.{i+1}", variant="secondary", scale=1)
                    chapter_generate_buttons.append(gen_btn)
                    chapter_delete_buttons.append(del_btn)
            
            # Add the "Generate All" button
            generate_all_btn = gr.Button("Generate All Chapters", variant="primary")
    
        with gr.Column(scale=3):
            # Chapter content
            chapter_title = gr.Textbox(label="Chapter Title", value="Chapter 1")
            gen_text_input = gr.Textbox(label="Text to Generate", lines=15)
    
    # Combined audio for all chapters
    with gr.Row():
        audio_output = gr.Audio(label="Generated Audio")
        spectrogram_output = gr.Image(label="Spectrogram")
    
    with gr.Row():
        with gr.Column(scale=3):
            combine_all_btn = gr.Button("Combine All Chapters", variant="secondary")
        with gr.Column(scale=1):
            clear_combined_btn = gr.Button("Clear Combined Audio", variant="secondary")
        
    combined_audio_output = gr.Audio(label="All Chapters Combined")
    
    # Chapter status table without delete action column
    gr.Markdown("## Chapter Status")
    chapter_status = gr.DataFrame(
        headers=["Chapter", "Status", "Length"],
        value=[[f"Chapter {i+1}", "Not Generated", "0s"] for i in range(7)]
    )
    
    with gr.Accordion("Advanced Settings", open=False):
        remove_silence = gr.Checkbox(
            label="Remove Silences",
            info="The model tends to produce silences, especially on longer audio. We can manually remove silences if needed. Note that this is an experimental feature and may produce strange results. This will also increase generation time.",
            value=False,
        )
        speed_slider = gr.Slider(
            label="Speed",
            minimum=0.3,
            maximum=2.0,
            value=1.0,
            step=0.1,
            info="Adjust the speed of the audio.",
        )
        nfe_slider = gr.Slider(
            label="NFE Steps",
            minimum=4,
            maximum=64,
            value=34,
            step=2,
            info="Set the number of denoising steps.",
        )
        cross_fade_duration_slider = gr.Slider(
            label="Cross-Fade Duration (s)",
            minimum=0.0,
            maximum=1.0,
            value=0.15,
            step=0.01,
            info="Set the duration of the cross-fade between audio clips.",
        )

    # Functions for chapter operations
    def switch_to_chapter(chapter_index):
        """Switch to a specific chapter"""
        chapter_state.current_chapter = chapter_index
        current_chapter = chapter_state.get_current_chapter()
        
        # Update UI with chapter data
        return (
            current_chapter["content"], 
            f"Chapter {chapter_index + 1}", 
            current_chapter["audio"] if current_chapter["generated"] else None,
            current_chapter["spectrogram"] if current_chapter["generated"] else None
        )
    
    def update_chapter_content(content):
        """Update the current chapter's content when text changes"""
        chapter_state.set_current_chapter_content(content)
        return content
    
    def generate_chapter_audio(ref_audio, ref_text, content, remove_silence, 
                             cross_fade, nfe_steps, speed):
        """Generate audio for the current chapter"""
        if not content.strip():
            gr.Warning("Please enter text for this chapter.")
            return None, None, ref_text, update_status_table()
        
        # Generate audio using the global tts model choice
        audio, spectrogram, ref_text_out = infer(
            ref_audio, ref_text, content, tts_model_choice, remove_silence,
            cross_fade, nfe_steps, speed, show_info=gr.Info
        )
        
        # Update chapter state
        chapter_state.set_current_chapter_audio(audio, spectrogram)
        
        # Update status table
        return audio, spectrogram, ref_text_out, update_status_table()
    
    def generate_specific_chapter(chapter_index, ref_audio, ref_text, remove_silence, 
                                cross_fade, nfe_steps, speed):
        """Generate audio for a specific chapter"""
        chapter = chapter_state.get_chapter(chapter_index)
        content = chapter["content"]
        
        if not content.strip():
            gr.Warning(f"Please enter text for Chapter {chapter_index+1}.")
            return None, None, ref_text, update_status_table()
        
        # Generate audio using the global tts model choice
        audio, spectrogram, ref_text_out = infer(
            ref_audio, ref_text, content, tts_model_choice, remove_silence,
            cross_fade, nfe_steps, speed, show_info=print
        )
        
        # Update chapter state
        chapter_state.set_chapter_audio(chapter_index, audio, spectrogram)
        
        # If this is the current chapter, update UI
        if chapter_state.current_chapter == chapter_index:
            return audio, spectrogram, ref_text_out, update_status_table()
        else:
            return gr.update(), gr.update(), ref_text_out, update_status_table()
    
    def generate_all_chapters(ref_audio, ref_text, remove_silence, 
                            cross_fade, nfe_steps, speed):
        """Generate audio for all chapters that have content"""
        if not ref_audio:
            gr.Warning("Please provide reference audio before generating.")
            return None, None, ref_text, update_status_table()
        
        last_audio = None
        last_spectrogram = None
        
        for i in range(len(chapter_state.chapters)):
            chapter = chapter_state.get_chapter(i)
            content = chapter["content"]
            
            if content.strip():
                # Generate audio for this chapter
                gr.Info(f"Generating Chapter {i+1}...")
                audio, spectrogram, ref_text = infer(
                    ref_audio, ref_text, content, tts_model_choice, remove_silence,
                    cross_fade, nfe_steps, speed, show_info=print
                )
                
                # Update chapter state
                chapter_state.set_chapter_audio(i, audio, spectrogram)
                
                # Remember the last generated audio for display
                last_audio = audio
                last_spectrogram = spectrogram
                
        # Display the current chapter's audio at the end
        current_chapter = chapter_state.get_current_chapter()
        audio_to_display = current_chapter["audio"] if current_chapter["generated"] else last_audio
        spectrogram_to_display = current_chapter["spectrogram"] if current_chapter["generated"] else last_spectrogram
        
        return audio_to_display, spectrogram_to_display, ref_text, update_status_table()
        
    def update_status_table():
        """Update the chapter status table"""
        status_data = []
        
        for i, chapter in enumerate(chapter_state.chapters):
            status = "Generated" if chapter["generated"] else "Not Generated"
            
            # Calculate length if audio is available
            length = "0s"
            if chapter["generated"] and chapter["audio"] is not None:
                sample_rate, audio_data = chapter["audio"]
                duration_seconds = len(audio_data) / sample_rate
                length = f"{duration_seconds:.1f}s"
                
            status_data.append([f"Chapter {i+1}", status, length])
            
        return status_data
    
    def delete_chapter_audio(chapter_index):
        """Delete the audio for a specific chapter"""
        chapter_state.delete_chapter_audio(chapter_index)
        
        # If this is the current chapter, update UI
        if chapter_state.current_chapter == chapter_index:
            return None, None, update_status_table()
        else:
            return gr.update(), gr.update(), update_status_table()
    
    def combine_all_chapters():
        """Combine all generated chapter audio into a single file"""
        generated_audios = []
        sample_rate = None
        
        # Collect all generated audio
        for chapter in chapter_state.chapters:
            if chapter["generated"] and chapter["audio"] is not None:
                sr, audio_data = chapter["audio"]
                generated_audios.append(audio_data)
                if sample_rate is None:
                    sample_rate = sr
        
        if not generated_audios:
            gr.Warning("No generated chapters to combine.")
            return None
        
        # Combine with silences between chapters
        silence_duration = 1.0  # 1 second silence between chapters
        silence_samples = int(silence_duration * sample_rate)
        silence = np.zeros(silence_samples)
        
        combined = generated_audios[0]
        for audio in generated_audios[1:]:
            combined = np.concatenate([combined, silence, audio])
        
        return (sample_rate, combined)
    
    def clear_combined_audio():
        """Clear the combined audio output"""
        return None
    
    # Wire up the chapter buttons
    for i, btn in enumerate(chapter_buttons):
        btn.click(
            switch_to_chapter, 
            inputs=[gr.Number(value=i, visible=False)], 
            outputs=[gen_text_input, chapter_title, audio_output, spectrogram_output]
        )
    
    # Update chapter content when text changes
    gen_text_input.change(
        update_chapter_content, 
        inputs=[gen_text_input], 
        outputs=[]  # Tidak perlu output karena kita hanya menyimpan state
    )
    
    # Tambahkan juga event untuk text input ketika user mengetik
    gen_text_input.input(
        update_chapter_content,
        inputs=[gen_text_input],
        outputs=[]
    )
    
    # Wire up the chapter generate buttons
    for i, gen_btn in enumerate(chapter_generate_buttons):
        gen_btn.click(
            generate_specific_chapter,
            inputs=[
                gr.Number(value=i, visible=False),
                ref_audio_input,
                ref_text_input,
                remove_silence,
                cross_fade_duration_slider,
                nfe_slider,
                speed_slider
            ],
            outputs=[audio_output, spectrogram_output, ref_text_input, chapter_status]
        )
        
    # Wire up the chapter delete buttons
    for i, del_btn in enumerate(chapter_delete_buttons):
        del_btn.click(
            delete_chapter_audio,
            inputs=[gr.Number(value=i, visible=False)],
            outputs=[audio_output, spectrogram_output, chapter_status]
        )
    
    # Wire up the "Generate All Chapters" button
    generate_all_btn.click(
        generate_all_chapters,
        inputs=[
            ref_audio_input,
            ref_text_input,
            remove_silence,
            cross_fade_duration_slider,
            nfe_slider,
            speed_slider
        ],
        outputs=[audio_output, spectrogram_output, ref_text_input, chapter_status]
    )
    
    # Combine all chapters button
    combine_all_btn.click(
        combine_all_chapters,
        inputs=[],
        outputs=[combined_audio_output]
    )

    # Clear combined audio button
    clear_combined_btn.click(
        clear_combined_audio,
        inputs=[],
        outputs=[combined_audio_output]
    )

    @gpu_decorator
    def basic_tts(
        ref_audio_input,
        ref_text_input,
        gen_text_input,
        remove_silence,
        cross_fade_duration_slider,
        nfe_slider,
        speed_slider,
    ):
        audio_out, spectrogram_path, ref_text_out = infer(
            ref_audio_input,
            ref_text_input,
            gen_text_input,
            tts_model_choice,
            remove_silence,
            cross_fade_duration=cross_fade_duration_slider,
            nfe_step=nfe_slider,
            speed=speed_slider,
        )
        return audio_out, spectrogram_path, ref_text_out

def parse_speechtypes_text(gen_text):
    # Pattern to find {speechtype}
    pattern = r"\{(.*?)\}"

    # Split the text by the pattern
    tokens = re.split(pattern, gen_text)

    segments = []

    current_style = "Regular"

    for i in range(len(tokens)):
        if i % 2 == 0:
            # This is text
            text = tokens[i].strip()
            if text:
                segments.append({"style": current_style, "text": text})
        else:
            # This is style
            style = tokens[i].strip()
            current_style = style

    return segments


with gr.Blocks() as app_multistyle:
    # New section for multistyle generation
    gr.Markdown(
        """
    # Multiple Speech-Type Generation

    This section allows you to generate multiple speech types or multiple people's voices. Enter your text in the format shown below, and the system will generate speech using the appropriate type. If unspecified, the model will use the regular speech type. The current speech type will be used until the next speech type is specified.
    """
    )

    with gr.Row():
        gr.Markdown(
            """
            **Example Input:**                                                                      
            {Regular} Hello, I'd like to order a sandwich please.                                                         
            {Surprised} What do you mean you're out of bread?                                                                      
            {Sad} I really wanted a sandwich though...                                                              
            {Angry} You know what, darn you and your little shop!                                                                       
            {Whisper} I'll just go back home and cry now.                                                                           
            {Shouting} Why me?!                                                                         
            """
        )

        gr.Markdown(
            """
            **Example Input 2:**                                                                                
            {Speaker1_Happy} Hello, I'd like to order a sandwich please.                                                            
            {Speaker2_Regular} Sorry, we're out of bread.                                                                                
            {Speaker1_Sad} I really wanted a sandwich though...                                                                             
            {Speaker2_Whisper} I'll give you the last one I was hiding.                                                                     
            """
        )

    gr.Markdown(
        "Upload different audio clips for each speech type. The first speech type is mandatory. You can add additional speech types by clicking the 'Add Speech Type' button."
    )

    # Regular speech type (mandatory)
    with gr.Row() as regular_row:
        with gr.Column():
            regular_name = gr.Textbox(value="Regular", label="Speech Type Name")
            regular_insert = gr.Button("Insert Label", variant="secondary")
        regular_audio = gr.Audio(label="Regular Reference Audio", type="filepath")
        regular_ref_text = gr.Textbox(label="Reference Text (Regular)", lines=2)

    # Regular speech type (max 100)
    max_speech_types = 100
    speech_type_rows = [regular_row]
    speech_type_names = [regular_name]
    speech_type_audios = [regular_audio]
    speech_type_ref_texts = [regular_ref_text]
    speech_type_delete_btns = [None]
    speech_type_insert_btns = [regular_insert]

    # Additional speech types (99 more)
    for i in range(max_speech_types - 1):
        with gr.Row(visible=False) as row:
            with gr.Column():
                name_input = gr.Textbox(label="Speech Type Name")
                delete_btn = gr.Button("Delete Type", variant="secondary")
                insert_btn = gr.Button("Insert Label", variant="secondary")
            audio_input = gr.Audio(label="Reference Audio", type="filepath")
            ref_text_input = gr.Textbox(label="Reference Text", lines=2)
        speech_type_rows.append(row)
        speech_type_names.append(name_input)
        speech_type_audios.append(audio_input)
        speech_type_ref_texts.append(ref_text_input)
        speech_type_delete_btns.append(delete_btn)
        speech_type_insert_btns.append(insert_btn)

    # Button to add speech type
    add_speech_type_btn = gr.Button("Add Speech Type")

    # Keep track of autoincrement of speech types, no roll back
    speech_type_count = 1

    # Function to add a speech type
    def add_speech_type_fn():
        row_updates = [gr.update() for _ in range(max_speech_types)]
        global speech_type_count
        if speech_type_count < max_speech_types:
            row_updates[speech_type_count] = gr.update(visible=True)
            speech_type_count += 1
        else:
            gr.Warning("Exhausted maximum number of speech types. Consider restart the app.")
        return row_updates

    add_speech_type_btn.click(add_speech_type_fn, outputs=speech_type_rows)

    # Function to delete a speech type
    def delete_speech_type_fn():
        return gr.update(visible=False), None, None, None

    # Update delete button clicks
    for i in range(1, len(speech_type_delete_btns)):
        speech_type_delete_btns[i].click(
            delete_speech_type_fn,
            outputs=[speech_type_rows[i], speech_type_names[i], speech_type_audios[i], speech_type_ref_texts[i]],
        )

    # Text input for the prompt
    gen_text_input_multistyle = gr.Textbox(
        label="Text to Generate",
        lines=10,
        placeholder="Enter the script with speaker names (or emotion types) at the start of each block, e.g.:\n\n{Regular} Hello, I'd like to order a sandwich please.\n{Surprised} What do you mean you're out of bread?\n{Sad} I really wanted a sandwich though...\n{Angry} You know what, darn you and your little shop!\n{Whisper} I'll just go back home and cry now.\n{Shouting} Why me?!",
    )

    def make_insert_speech_type_fn(index):
        def insert_speech_type_fn(current_text, speech_type_name):
            current_text = current_text or ""
            speech_type_name = speech_type_name or "None"
            updated_text = current_text + f"{{{speech_type_name}}} "
            return updated_text

        return insert_speech_type_fn

    for i, insert_btn in enumerate(speech_type_insert_btns):
        insert_fn = make_insert_speech_type_fn(i)
        insert_btn.click(
            insert_fn,
            inputs=[gen_text_input_multistyle, speech_type_names[i]],
            outputs=gen_text_input_multistyle,
        )

    with gr.Accordion("Advanced Settings", open=False):
        remove_silence_multistyle = gr.Checkbox(
            label="Remove Silences",
            value=True,
        )

    # Generate button
    generate_multistyle_btn = gr.Button("Generate Multi-Style Speech", variant="primary")

    # Output audio
    audio_output_multistyle = gr.Audio(label="Synthesized Audio")

    @gpu_decorator
    def generate_multistyle_speech(
        gen_text,
        *args,
    ):
        speech_type_names_list = args[:max_speech_types]
        speech_type_audios_list = args[max_speech_types : 2 * max_speech_types]
        speech_type_ref_texts_list = args[2 * max_speech_types : 3 * max_speech_types]
        remove_silence = args[3 * max_speech_types]
        # Collect the speech types and their audios into a dict
        speech_types = OrderedDict()

        ref_text_idx = 0
        for name_input, audio_input, ref_text_input in zip(
            speech_type_names_list, speech_type_audios_list, speech_type_ref_texts_list
        ):
            if name_input and audio_input:
                speech_types[name_input] = {"audio": audio_input, "ref_text": ref_text_input}
            else:
                speech_types[f"@{ref_text_idx}@"] = {"audio": "", "ref_text": ""}
            ref_text_idx += 1

        # Parse the gen_text into segments
        segments = parse_speechtypes_text(gen_text)

        # For each segment, generate speech
        generated_audio_segments = []
        current_style = "Regular"

        for segment in segments:
            style = segment["style"]
            text = segment["text"]

            if style in speech_types:
                current_style = style
            else:
                gr.Warning(f"Type {style} is not available, will use Regular as default.")
                current_style = "Regular"

            try:
                ref_audio = speech_types[current_style]["audio"]
            except KeyError:
                gr.Warning(f"Please provide reference audio for type {current_style}.")
                return [None] + [speech_types[style]["ref_text"] for style in speech_types]
            ref_text = speech_types[current_style].get("ref_text", "")

            # Generate speech for this segment
            audio_out, _, ref_text_out = infer(
                ref_audio, ref_text, text, tts_model_choice, remove_silence, 0, show_info=print
            )  # show_info=print no pull to top when generating
            sr, audio_data = audio_out

            generated_audio_segments.append(audio_data)
            speech_types[current_style]["ref_text"] = ref_text_out

        # Concatenate all audio segments
        if generated_audio_segments:
            final_audio_data = np.concatenate(generated_audio_segments)
            return [(sr, final_audio_data)] + [speech_types[style]["ref_text"] for style in speech_types]
        else:
            gr.Warning("No audio generated.")
            return [None] + [speech_types[style]["ref_text"] for style in speech_types]

    generate_multistyle_btn.click(
        generate_multistyle_speech,
        inputs=[
            gen_text_input_multistyle,
        ]
        + speech_type_names
        + speech_type_audios
        + speech_type_ref_texts
        + [
            remove_silence_multistyle,
        ],
        outputs=[audio_output_multistyle] + speech_type_ref_texts,
    )

    # Validation function to disable Generate button if speech types are missing
    def validate_speech_types(gen_text, regular_name, *args):
        speech_type_names_list = args

        # Collect the speech types names
        speech_types_available = set()
        if regular_name:
            speech_types_available.add(regular_name)
        for name_input in speech_type_names_list:
            if name_input:
                speech_types_available.add(name_input)

        # Parse the gen_text to get the speech types used
        segments = parse_speechtypes_text(gen_text)
        speech_types_in_text = set(segment["style"] for segment in segments)

        # Check if all speech types in text are available
        missing_speech_types = speech_types_in_text - speech_types_available

        if missing_speech_types:
            # Disable the generate button
            return gr.update(interactive=False)
        else:
            # Enable the generate button
            return gr.update(interactive=True)

    gen_text_input_multistyle.change(
        validate_speech_types,
        inputs=[gen_text_input_multistyle, regular_name] + speech_type_names,
        outputs=generate_multistyle_btn,
    )


with gr.Blocks() as app_chat:
    gr.Markdown(
        """
# Voice Chat
Have a conversation with an AI using your reference voice! 
1. Upload a reference audio clip and optionally its transcript.
2. Load the chat model.
3. Record your message through your microphone.
4. The AI will respond using the reference voice.
"""
    )

    if not USING_SPACES:
        load_chat_model_btn = gr.Button("Load Chat Model", variant="primary")

        chat_interface_container = gr.Column(visible=False)

        @gpu_decorator
        def load_chat_model():
            global chat_model_state, chat_tokenizer_state
            if chat_model_state is None:
                show_info = gr.Info
                show_info("Loading chat model...")
                model_name = "Qwen/Qwen2.5-3B-Instruct"
                chat_model_state = AutoModelForCausalLM.from_pretrained(
                    model_name, torch_dtype="auto", device_map="auto"
                )
                chat_tokenizer_state = AutoTokenizer.from_pretrained(model_name)
                show_info("Chat model loaded.")

            return gr.update(visible=False), gr.update(visible=True)

        load_chat_model_btn.click(load_chat_model, outputs=[load_chat_model_btn, chat_interface_container])

    else:
        chat_interface_container = gr.Column()

        if chat_model_state is None:
            model_name = "Qwen/Qwen2.5-3B-Instruct"
            chat_model_state = AutoModelForCausalLM.from_pretrained(model_name, torch_dtype="auto", device_map="auto")
            chat_tokenizer_state = AutoTokenizer.from_pretrained(model_name)

    with chat_interface_container:
        with gr.Row():
            with gr.Column():
                ref_audio_chat = gr.Audio(label="Reference Audio", type="filepath")
            with gr.Column():
                with gr.Accordion("Advanced Settings", open=False):
                    remove_silence_chat = gr.Checkbox(
                        label="Remove Silences",
                        value=True,
                    )
                    ref_text_chat = gr.Textbox(
                        label="Reference Text",
                        info="Optional: Leave blank to auto-transcribe",
                        lines=2,
                    )
                    system_prompt_chat = gr.Textbox(
                        label="System Prompt",
                        value="You are not an AI assistant, you are whoever the user says you are. You must stay in character. Keep your responses concise since they will be spoken out loud.",
                        lines=2,
                    )

        chatbot_interface = gr.Chatbot(label="Conversation")

        with gr.Row():
            with gr.Column():
                audio_input_chat = gr.Microphone(
                    label="Speak your message",
                    type="filepath",
                )
                audio_output_chat = gr.Audio(autoplay=True)
            with gr.Column():
                text_input_chat = gr.Textbox(
                    label="Type your message",
                    lines=1,
                )
                send_btn_chat = gr.Button("Send Message")
                clear_btn_chat = gr.Button("Clear Conversation")

        conversation_state = gr.State(
            value=[
                {
                    "role": "system",
                    "content": "You are not an AI assistant, you are whoever the user says you are. You must stay in character. Keep your responses concise since they will be spoken out loud.",
                }
            ]
        )

        # Modify process_audio_input to use model and tokenizer from state
        @gpu_decorator
        def process_audio_input(audio_path, text, history, conv_state):
            """Handle audio or text input from user"""

            if not audio_path and not text.strip():
                return history, conv_state, ""

            if audio_path:
                text = preprocess_ref_audio_text(audio_path, text)[1]

            if not text.strip():
                return history, conv_state, ""

            conv_state.append({"role": "user", "content": text})
            history.append((text, None))

            response = generate_response(conv_state, chat_model_state, chat_tokenizer_state)

            conv_state.append({"role": "assistant", "content": response})
            history[-1] = (text, response)

            return history, conv_state, ""

        @gpu_decorator
        def generate_audio_response(history, ref_audio, ref_text, remove_silence):
            """Generate TTS audio for AI response"""
            if not history or not ref_audio:
                return None

            last_user_message, last_ai_response = history[-1]
            if not last_ai_response:
                return None

            audio_result, _, ref_text_out = infer(
                ref_audio,
                ref_text,
                last_ai_response,
                tts_model_choice,
                remove_silence,
                cross_fade_duration=0.15,
                speed=1.0,
                show_info=print,  # show_info=print no pull to top when generating
            )
            return audio_result, ref_text_out

        def clear_conversation():
            """Reset the conversation"""
            return [], [
                {
                    "role": "system",
                    "content": "You are not an AI assistant, you are whoever the user says you are. You must stay in character. Keep your responses concise since they will be spoken out loud.",
                }
            ]

        def update_system_prompt(new_prompt):
            """Update the system prompt and reset the conversation"""
            new_conv_state = [{"role": "system", "content": new_prompt}]
            return [], new_conv_state

        # Handle audio input
        audio_input_chat.stop_recording(
            process_audio_input,
            inputs=[audio_input_chat, text_input_chat, chatbot_interface, conversation_state],
            outputs=[chatbot_interface, conversation_state, text_input_chat],
        ).then(
            generate_audio_response,
            inputs=[chatbot_interface, ref_audio_chat, ref_text_chat, remove_silence_chat],
            outputs=[audio_output_chat, ref_text_chat],
        )

        # Handle text input
        text_input_chat.submit(
            process_audio_input,
            inputs=[audio_input_chat, text_input_chat, chatbot_interface, conversation_state],
            outputs=[chatbot_interface, conversation_state, text_input_chat],
        ).then(
            generate_audio_response,
            inputs=[chatbot_interface, ref_audio_chat, ref_text_chat, remove_silence_chat],
            outputs=[audio_output_chat, ref_text_chat],
        )

        # Handle send button
        send_btn_chat.click(
            process_audio_input,
            inputs=[audio_input_chat, text_input_chat, chatbot_interface, conversation_state],
            outputs=[chatbot_interface, conversation_state, text_input_chat],
        ).then(
            generate_audio_response,
            inputs=[chatbot_interface, ref_audio_chat, ref_text_chat, remove_silence_chat],
            outputs=[audio_output_chat, ref_text_chat],
        )

        # Handle clear button
        clear_btn_chat.click(
            clear_conversation,
            outputs=[chatbot_interface, conversation_state],
        )

        # Handle system prompt change and reset conversation
        system_prompt_chat.change(
            update_system_prompt,
            inputs=system_prompt_chat,
            outputs=[chatbot_interface, conversation_state],
        )


with gr.Blocks() as app:
    gr.Markdown(
        f"""
# E2/F5 TTS

This is {"a local web UI for [F5 TTS](https://github.com/SWivid/F5-TTS)" if not USING_SPACES else "an online demo for [F5-TTS](https://github.com/SWivid/F5-TTS)"} with advanced batch processing support. This app supports the following TTS models:

* [F5-TTS](https://arxiv.org/abs/2410.06885) (A Fairytaler that Fakes Fluent and Faithful Speech with Flow Matching)
* [E2 TTS](https://arxiv.org/abs/2406.18009) (Embarrassingly Easy Fully Non-Autoregressive Zero-Shot TTS)

The checkpoints currently support English and Chinese.

If you're having issues, try converting your reference audio to WAV or MP3, clipping it to 15s with  ✂  in the bottom right corner (otherwise might have non-optimal auto-trimmed result).

**NOTE: Reference text will be automatically transcribed with Whisper if not provided. For best results, keep your reference clips short (<15s). Ensure the audio is fully uploaded before generating.**
"""
    )

    last_used_custom = files("f5_tts").joinpath("infer/.cache/last_used_custom_model_info.txt")

    def load_last_used_custom():
        try:
            custom = []
            with open(last_used_custom, "r", encoding="utf-8") as f:
                for line in f:
                    custom.append(line.strip())
            return custom
        except FileNotFoundError:
            last_used_custom.parent.mkdir(parents=True, exist_ok=True)
            return DEFAULT_TTS_MODEL_CFG

    def switch_tts_model(new_choice):
        global tts_model_choice
        if new_choice == "Custom":  # override in case webpage is refreshed
            custom_ckpt_path, custom_vocab_path, custom_model_cfg = load_last_used_custom()
            tts_model_choice = ["Custom", custom_ckpt_path, custom_vocab_path, json.loads(custom_model_cfg)]
            return (
                gr.update(visible=True, value=custom_ckpt_path),
                gr.update(visible=True, value=custom_vocab_path),
                gr.update(visible=True, value=custom_model_cfg),
            )
        else:
            tts_model_choice = new_choice
            return gr.update(visible=False), gr.update(visible=False), gr.update(visible=False)

    def set_custom_model(custom_ckpt_path, custom_vocab_path, custom_model_cfg):
        global tts_model_choice
        tts_model_choice = ["Custom", custom_ckpt_path, custom_vocab_path, json.loads(custom_model_cfg)]
        with open(last_used_custom, "w", encoding="utf-8") as f:
            f.write(custom_ckpt_path + "\n" + custom_vocab_path + "\n" + custom_model_cfg + "\n")

    with gr.Row():
        if not USING_SPACES:
            choose_tts_model = gr.Radio(
                choices=[DEFAULT_TTS_MODEL, "E2-TTS", "Custom"], label="Choose TTS Model", value=DEFAULT_TTS_MODEL
            )
        else:
            choose_tts_model = gr.Radio(
                choices=[DEFAULT_TTS_MODEL, "E2-TTS"], label="Choose TTS Model", value=DEFAULT_TTS_MODEL
            )
        custom_ckpt_path = gr.Dropdown(
            choices=[DEFAULT_TTS_MODEL_CFG[0]],
            value=load_last_used_custom()[0],
            allow_custom_value=True,
            label="Model: local_path | hf://user_id/repo_id/model_ckpt",
            visible=False,
        )
        custom_vocab_path = gr.Dropdown(
            choices=[DEFAULT_TTS_MODEL_CFG[1]],
            value=load_last_used_custom()[1],
            allow_custom_value=True,
            label="Vocab: local_path | hf://user_id/repo_id/vocab_file",
            visible=False,
        )
        custom_model_cfg = gr.Dropdown(
            choices=[
                DEFAULT_TTS_MODEL_CFG[2],
                json.dumps(dict(dim=768, depth=18, heads=12, ff_mult=2, text_dim=512, conv_layers=4)),
            ],
            value=load_last_used_custom()[2],
            allow_custom_value=True,
            label="Config: in a dictionary form",
            visible=False,
        )

    choose_tts_model.change(
        switch_tts_model,
        inputs=[choose_tts_model],
        outputs=[custom_ckpt_path, custom_vocab_path, custom_model_cfg],
        show_progress="hidden",
    )
    custom_ckpt_path.change(
        set_custom_model,
        inputs=[custom_ckpt_path, custom_vocab_path, custom_model_cfg],
        show_progress="hidden",
    )
    custom_vocab_path.change(
        set_custom_model,
        inputs=[custom_ckpt_path, custom_vocab_path, custom_model_cfg],
        show_progress="hidden",
    )
    custom_model_cfg.change(
        set_custom_model,
        inputs=[custom_ckpt_path, custom_vocab_path, custom_model_cfg],
        show_progress="hidden",
    )

    gr.TabbedInterface(
        [app_tts, app_multistyle, app_chat, app_credits],
        ["Basic-TTS", "Multi-Speech", "Voice-Chat", "Credits"],
    )


@click.command()
@click.option("--port", "-p", default=None, type=int, help="Port to run the app on")
@click.option("--host", "-H", default=None, help="Host to run the app on")
@click.option(
    "--share",
    "-s",
    default=False,
    is_flag=True,
    help="Share the app via Gradio share link",
)
@click.option("--api", "-a", default=True, is_flag=True, help="Allow API access")
@click.option(
    "--root_path",
    "-r",
    default=None,
    type=str,
    help='The root path (or "mount point") of the application, if it\'s not served from the root ("/") of the domain. Often used when the application is behind a reverse proxy that forwards requests to the application, e.g. set "/myapp" or full URL for application served at "https://example.com/myapp".',
)
@click.option(
    "--inbrowser",
    "-i",
    is_flag=True,
    default=False,
    help="Automatically launch the interface in the default web browser",
)
def main(port, host, share, api, root_path, inbrowser):
    global app
    print("Starting app...")
    app.queue(api_open=api).launch(
        server_name=host,
        server_port=port,
        share=share,
        show_api=api,
        root_path=root_path,
        inbrowser=inbrowser,
    )


if __name__ == "__main__":
    if not USING_SPACES:
        main()
    else:
        app.queue().launch()
