
import io
import numpy as np
import librosa
import soundfile as sf

TARGET_SAMPLE_RATE = 22050


def load_audio(audio_bytes):
    audio, sr = librosa.load(
        io.BytesIO(audio_bytes),
        sr=TARGET_SAMPLE_RATE,
        mono=True
    )

    if audio.size == 0:
        raise ValueError("Audio file is empty or could not be decoded.")

    return audio, sr


def extract_audio_features(audio, sr):
    rms = librosa.feature.rms(y=audio)[0]
    energy = float(np.mean(rms))

    pitch = librosa.yin(audio, fmin=65, fmax=400, sr=sr)
    pitch = pitch[np.isfinite(pitch)]

    pitch_std = float(np.std(pitch)) if pitch.size else 0.0
    silence_ratio = float(np.mean(rms < 0.01))
    duration = float(len(audio) / sr)

    return {
        "average_energy": energy,
        "pitch_std_hz": pitch_std,
        "silence_ratio": silence_ratio,
        "duration_seconds": duration
    }


def recommend_intensity(features):
    energy_score = np.clip(
        features["average_energy"] / 0.15, 0, 1
    )
    pitch_score = np.clip(
        features["pitch_std_hz"] / 80.0, 0, 1
    )

    score = 0.6 * energy_score + 0.4 * pitch_score
    intensity = 1.0 + 9.0 * score

    return round(float(np.clip(intensity, 1, 10)), 2)


def analyze_audio_for_aaec(audio_bytes):
    audio, sr = load_audio(audio_bytes)
    features = extract_audio_features(audio, sr)

    return {
        "features": features,
        "recommended_intensity": recommend_intensity(features)
    }


def analyze_audio_readiness(audio_bytes):
    metadata = sf.info(io.BytesIO(audio_bytes))
    audio, sr = load_audio(audio_bytes)

    duration = len(audio) / sr
    rms = librosa.feature.rms(y=audio)[0]

    silence_ratio = float(np.mean(rms < 0.01))
    clipping_ratio = float(np.mean(np.abs(audio) >= 0.99))

    warnings = []

    if duration < 2:
        warnings.append("Audio is very short.")

    if silence_ratio > 0.40:
        warnings.append("Audio contains a high proportion of silence.")

    if clipping_ratio > 0.01:
        warnings.append("Audio may contain clipping or distortion.")

    if metadata.samplerate != TARGET_SAMPLE_RATE:
        warnings.append(
            f"Audio is resampled to {TARGET_SAMPLE_RATE} Hz for analysis."
        )

    if metadata.channels > 1:
        warnings.append("Stereo audio is converted to mono for analysis.")

    return {
        "ready": len(warnings) == 0,
        "duration_seconds": round(duration, 2),
        "original_sample_rate": metadata.samplerate,
        "channels": metadata.channels,
        "silence_percentage": round(silence_ratio * 100, 2),
        "clipping_percentage": round(clipping_ratio * 100, 2),
        "warnings": warnings
    }


def normalize_audio(audio_bytes):
    audio, sr = load_audio(audio_bytes)

    peak = float(np.max(np.abs(audio)))

    if peak > 0:
        audio = audio / peak * 0.95

    output = io.BytesIO()
    sf.write(output, audio, sr, format="WAV")
    output.seek(0)

    return output.read()
