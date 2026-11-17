"""Схема таймлайна. Всё, что агенты (режиссёр, монтажёр, моушн) отдают в рендер."""
from typing import List, Literal, Optional, Union

from pydantic import BaseModel, Field


class Scene(BaseModel):
    src: str                                   # URL видео или картинки
    type: Literal["auto", "video", "image"] = "auto"
    duration: float = Field(gt=0, le=60)       # сколько сцена висит на таймлайне, сек
    trim_start: float = 0                      # с какой секунды брать видео
    fit: Literal["cover", "blur"] = "cover"    # cover — обрезать под 9:16, blur — вписать на размытый фон
    kenburns: Literal["none", "in", "out", "left", "right"] = "in"   # только для картинок
    transition: str = "none"                   # переход В СЛЕДУЮЩУЮ сцену: none | fade | fadeblack | slideleft | slideup | wipeleft | circleopen | zoomin | smoothleft
    transition_duration: float = 0.35
    keep_audio: bool = False                   # оставить родной звук клипа (речь в кадре)
    audio_volume: float = 1.0


class Word(BaseModel):
    w: str
    start: float
    end: float


class CaptionSegment(BaseModel):
    start: float
    end: float
    text: str
    words: Optional[List[Word]] = None         # если есть — подсветка слова под голос


class TextStyle(BaseModel):
    font: str = "Montserrat ExtraBold"
    size: int = 0                              # 0 = размер по умолчанию для типа
    color: str = "#FFFFFF"
    accent: str = "#FFD400"                    # подсветка слова, цифры, плашки
    outline: str = "#000000"
    outline_width: float = 6
    box: bool = False                          # подложка-плашка под текстом
    box_color: str = "#000000"
    box_opacity: float = 0.65
    uppercase: bool = False


class Captions(BaseModel):
    segments: List[CaptionSegment] = []
    position: Literal["bottom", "center", "top"] = "bottom"
    highlight: bool = True                     # подсветка текущего слова (нужны words)
    pop: bool = True                           # лёгкий «поп» на появлении
    max_chars: int = 22                        # перенос строки
    y: Optional[int] = None                    # центр строки по Y в px (перекрывает position) — например, на месте титров рефа
    style: TextStyle = TextStyle()


class TextOverlay(BaseModel):
    kind: Literal["title", "plate", "stat", "list", "cta"] = "title"
    text: str = ""                             # title/plate/cta: текст; stat: подпись под цифрой
    value: Optional[str] = None                # stat: «+35%», «1 200 ₽», «12 лет» — цифра считается от нуля
    items: List[str] = []                      # list: пункты
    start: float = 0
    end: float = -1                            # -1 = до конца ролика
    position: Literal["top", "center", "bottom", "upper", "lower"] = "center"
    stagger: float = 0.45                      # list: пауза между пунктами
    style: TextStyle = TextStyle()


class ImageOverlay(BaseModel):
    src: str                                   # PNG с прозрачностью — логотип, стикер, график
    start: float = 0
    end: float = -1
    x: Union[int, str] = "(W-w)/2"             # выражение ffmpeg или пиксели
    y: Union[int, str] = 120
    width: int = 260
    fade: float = 0.25


class BlurRegion(BaseModel):
    """Размытая зона поверх видео: прячет чужие титры, логотипы, водяные знаки."""
    x: int = 0                                 # левый край зоны, px
    y: int                                     # верхний край зоны, px
    w: int = 1080                              # ширина, px (1080 = полоса на всю ширину)
    h: int                                     # высота, px
    start: float = 0
    end: float = -1                            # -1 = до конца ролика
    strength: int = 28                         # сила размытия
    feather: int = 36                          # мягкость краёв наружу от зоны, px
    dim: float = 0.25                          # затемнение размытого 0..0.8 (читаемость наших титров)


class AudioTrack(BaseModel):
    src: str
    volume: float = 1.0
    start: float = 0                           # где начинается на таймлайне


class Music(AudioTrack):
    volume: float = 0.18
    duck: bool = True                          # приглушать под голосом
    trim_start: float = 0
    fade_in: float = 0.5
    fade_out: float = 1.5


class Sfx(BaseModel):
    src: str
    at: float
    volume: float = 0.8


class RenderRequest(BaseModel):
    width: int = 1080
    height: int = 1920
    fps: int = 30
    scenes: List[Scene]
    voiceover: Optional[AudioTrack] = None
    music: Optional[Music] = None
    sfx: List[Sfx] = []
    captions: Optional[Captions] = None
    texts: List[TextOverlay] = []
    images: List[ImageOverlay] = []
    blurs: List[BlurRegion] = []               # размытые зоны (ложатся раньше картинок и текста)
    loudness: float = -14                      # LUFS, норма для Reels/Shorts
    quality: Literal["draft", "final"] = "final"
    callback_url: Optional[str] = None
    meta: dict = {}                            # прокидывается в колбэк как есть (project_id, chat_id…)
