# Add this to your entry_points section:
entry_points={
    'console_scripts': [
        # Add this line to your existing entry points
        'f5-tts_chapters-gradio=f5_tts.infer.infer_chapters_gradio:main',
    ],
},