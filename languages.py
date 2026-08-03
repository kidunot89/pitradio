"""Whisper languages, model sizes, and how the two combine into a model name.

Pure data and pure logic, so config validation and the language tab both work
off it and it stays testable anywhere.

There is no such thing as a per-language Whisper model. There are English-only
builds (`tiny.en` … `medium.en`) and multilingual builds (`tiny` … `large-v3`),
and every multilingual build handles every language Whisper knows. Choosing a size per
language is still worth doing: multilingual `small` is meaningfully weaker than
`small.en`, so wanting a larger model for a second language is reasonable.
"""

from __future__ import annotations

# Size -> (approximate download, note). Sizes are the user-facing choice; the
# actual model name is derived by model_name().
SIZES: dict[str, tuple[str, str]] = {
    "tiny": ("~75 MB", "fastest, least accurate"),
    "base": ("~145 MB", "fast"),
    "small": ("~480 MB", "the default; a good balance on CPU"),
    "medium": ("~1.5 GB", "noticeably slower on CPU"),
    "large": ("~3 GB", "slowest; often too slow to use mid-stint"),
}

DEFAULT_SIZE = "small"

# Sizes that ship an English-only variant. large has none.
ENGLISH_ONLY_SIZES = ("tiny", "base", "small", "medium")

# The multilingual large build to use when someone picks "large". Pinned rather
# than tracking "latest" so an upgrade never silently changes what gets
# downloaded onto someone's machine.
LARGE_MODEL = "large-v3"


def model_name(language: str, size: str) -> str:
    """The faster-whisper model for a language and size.

    English gets the `.en` build where one exists: at the same size and speed
    it is more accurate than the multilingual model, which is the whole reason
    to special-case it.
    """
    size = (size or DEFAULT_SIZE).strip().lower()
    if size not in SIZES:
        size = DEFAULT_SIZE
    if size == "large":
        return LARGE_MODEL
    if (language or "").strip().lower() == "en" and size in ENGLISH_ONLY_SIZES:
        return f"{size}.en"
    return size


def size_of(model: str) -> str:
    """Inverse of model_name, for showing an existing config in the UI."""
    model = (model or "").strip().lower()
    if model.startswith("large"):
        return "large"
    base = model[:-3] if model.endswith(".en") else model
    return base if base in SIZES else DEFAULT_SIZE


# The languages Whisper is trained on. Codes are what faster-whisper expects.
WHISPER_LANGUAGES: dict[str, str] = {
    "en": "English", "zh": "Chinese", "de": "German", "es": "Spanish",
    "ru": "Russian", "ko": "Korean", "fr": "French", "ja": "Japanese",
    "pt": "Portuguese", "tr": "Turkish", "pl": "Polish", "ca": "Catalan",
    "nl": "Dutch", "ar": "Arabic", "sv": "Swedish", "it": "Italian",
    "id": "Indonesian", "hi": "Hindi", "fi": "Finnish", "vi": "Vietnamese",
    "he": "Hebrew", "uk": "Ukrainian", "el": "Greek", "ms": "Malay",
    "cs": "Czech", "ro": "Romanian", "da": "Danish", "hu": "Hungarian",
    "ta": "Tamil", "no": "Norwegian", "th": "Thai", "ur": "Urdu",
    "hr": "Croatian", "bg": "Bulgarian", "lt": "Lithuanian", "la": "Latin",
    "mi": "Maori", "ml": "Malayalam", "cy": "Welsh", "sk": "Slovak",
    "te": "Telugu", "fa": "Persian", "lv": "Latvian", "bn": "Bengali",
    "sr": "Serbian", "az": "Azerbaijani", "sl": "Slovenian", "kn": "Kannada",
    "et": "Estonian", "mk": "Macedonian", "br": "Breton", "eu": "Basque",
    "is": "Icelandic", "hy": "Armenian", "ne": "Nepali", "mn": "Mongolian",
    "bs": "Bosnian", "kk": "Kazakh", "sq": "Albanian", "sw": "Swahili",
    "gl": "Galician", "mr": "Marathi", "pa": "Punjabi", "si": "Sinhala",
    "km": "Khmer", "sn": "Shona", "yo": "Yoruba", "so": "Somali",
    "af": "Afrikaans", "oc": "Occitan", "ka": "Georgian", "be": "Belarusian",
    "tg": "Tajik", "sd": "Sindhi", "gu": "Gujarati", "am": "Amharic",
    "yi": "Yiddish", "lo": "Lao", "uz": "Uzbek", "fo": "Faroese",
    "ht": "Haitian Creole", "ps": "Pashto", "tk": "Turkmen", "nn": "Nynorsk",
    "mt": "Maltese", "sa": "Sanskrit", "lb": "Luxembourgish", "my": "Burmese",
    "bo": "Tibetan", "tl": "Tagalog", "mg": "Malagasy", "as": "Assamese",
    "tt": "Tatar", "haw": "Hawaiian", "ln": "Lingala", "ha": "Hausa",
    "ba": "Bashkir", "jw": "Javanese", "su": "Sundanese", "yue": "Cantonese",
}


def language_name(code: str) -> str:
    return WHISPER_LANGUAGES.get((code or "").strip().lower(), code)


def label(code: str) -> str:
    """'English (en)' — what the picker shows."""
    return f"{language_name(code)} ({code})"


def code_from_label(text: str) -> str:
    """Recover the code from a picker label."""
    text = (text or "").strip()
    if text.endswith(")") and "(" in text:
        return text.rsplit("(", 1)[1].rstrip(")").strip().lower()
    return text.lower()


def sorted_labels() -> list[str]:
    """English first, then alphabetical: the default should be easy to find."""
    codes = sorted(WHISPER_LANGUAGES, key=lambda c: (c != "en", WHISPER_LANGUAGES[c]))
    return [label(code) for code in codes]
