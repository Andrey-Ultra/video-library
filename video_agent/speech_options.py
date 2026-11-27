"""Speech capabilities shared by validation and the native settings UI."""

PARAKEET_V3 = "mlx-community/parakeet-tdt-0.6b-v3"
PARAKEET_V2 = "mlx-community/parakeet-tdt-0.6b-v2"
PARAKEET_LANGUAGES = {
    "en": "English",
    "ru": "Russian",
    "uk": "Ukrainian",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "pl": "Polish",
    "nl": "Dutch",
    "sv": "Swedish",
    "da": "Danish",
    "fi": "Finnish",
    "cs": "Czech",
    "el": "Greek",
    "ro": "Romanian",
    "hu": "Hungarian",
    "bg": "Bulgarian",
    "hr": "Croatian",
    "sk": "Slovak",
    "sl": "Slovenian",
    "et": "Estonian",
    "lv": "Latvian",
    "lt": "Lithuanian",
    "mt": "Maltese",
}
# A curated selection; Auto also recognizes other languages supported by the model.
WHISPER_LANGUAGES = {
    **PARAKEET_LANGUAGES,
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "ar": "Arabic",
    "hi": "Hindi",
    "tr": "Turkish",
    "id": "Indonesian",
    "vi": "Vietnamese",
    "th": "Thai",
    "he": "Hebrew",
    "fa": "Persian",
    "bn": "Bengali",
    "ta": "Tamil",
    "ur": "Urdu",
}


def speech_options():
    return {
        "parakeet_models": [
            {
                "id": PARAKEET_V3,
                "name": "Parakeet v3 · Multilingual (recommended)",
                "languages": list(PARAKEET_LANGUAGES.values()),
            },
            {"id": PARAKEET_V2, "name": "Parakeet v2 · English only", "languages": ["English"]},
        ],
        "whisper_languages": [{"code": code, "name": name} for code, name in WHISPER_LANGUAGES.items()],
    }
