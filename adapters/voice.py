"""Voice I/O adapter — microphone + macOS TTS."""

from __future__ import annotations

import asyncio
import platform
import subprocess


class VoiceIO:
    """Microphone input (speech_recognition) + macOS `say` output."""

    def __init__(self, language: str = "zh-CN", tts_voice: str = ""):
        self.language = language
        self.tts_voice = tts_voice
        self._recognizer = None
        self._sr = None
        self._mic_ready = False
        self._tts_available = platform.system() == "Darwin"
        self._init_mic()

    def _init_mic(self) -> None:
        try:
            import speech_recognition as sr

            self._sr = sr
            self._recognizer = sr.Recognizer()
            self._recognizer.dynamic_energy_threshold = True
            with sr.Microphone() as source:
                self._recognizer.adjust_for_ambient_noise(source, duration=0.5)
            self._mic_ready = True
            print("[voice] 麦克风就绪")
        except ImportError:
            print("[voice] 未安装 SpeechRecognition，语音输入不可用")
            print("        安装: brew install portaudio && pip install SpeechRecognition pyaudio")
        except Exception as e:
            print(f"[voice] 麦克风初始化失败: {e}")

    async def listen(self) -> str | None:
        """录音 → 语音识别 → 返回文字。无法识别时返回 None。"""
        if not self._mic_ready:
            # 降级到键盘
            try:
                return input("你: ").strip() or None
            except (EOFError, KeyboardInterrupt):
                return None

        sr = self._sr
        recognizer = self._recognizer

        def _record() -> str | None:
            try:
                with sr.Microphone() as source:
                    print("(听...) ", end="", flush=True)
                    audio = recognizer.listen(source, phrase_time_limit=30)
                    text = recognizer.recognize_google(audio, language=self.language)
                    print(f"\r你: {text}")
                    return text
            except sr.UnknownValueError:
                print("\r(没听清)")
                return None
            except sr.RequestError as e:
                print(f"\r[voice] 识别服务错误: {e}")
                return None

        return await asyncio.to_thread(_record)

    async def speak(self, text: str) -> None:
        """用 macOS say 命令朗读文本。"""
        if not self._tts_available or not text:
            print(f"Roboot: {text}")
            return

        print(f"Roboot: {text}")

        cmd = ["say"]
        if self.tts_voice:
            cmd.extend(["-v", self.tts_voice])
        cmd.append(text)

        try:
            await asyncio.to_thread(
                subprocess.run, cmd, timeout=60, check=False
            )
        except Exception:
            pass  # TTS failure is non-critical
