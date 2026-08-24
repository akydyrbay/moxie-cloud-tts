from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor
REMOTECHAT_TTS_REQUEST: RequestSourceType
ROBOT_TTS_REQUEST: RequestSourceType

class AudioBuffer(_message.Message):
    __slots__ = ["buffer", "channels", "sample_rate"]
    BUFFER_FIELD_NUMBER: _ClassVar[int]
    CHANNELS_FIELD_NUMBER: _ClassVar[int]
    SAMPLE_RATE_FIELD_NUMBER: _ClassVar[int]
    buffer: bytes
    channels: int
    sample_rate: int
    def __init__(self, buffer: _Optional[bytes] = ..., channels: _Optional[int] = ..., sample_rate: _Optional[int] = ...) -> None: ...

class CloudTTSRequest(_message.Message):
    __slots__ = ["chunk_num", "event_id", "markup", "module_name", "software_version", "timestamp", "user_id"]
    CHUNK_NUM_FIELD_NUMBER: _ClassVar[int]
    EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    MARKUP_FIELD_NUMBER: _ClassVar[int]
    MODULE_NAME_FIELD_NUMBER: _ClassVar[int]
    SOFTWARE_VERSION_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    chunk_num: int
    event_id: str
    markup: str
    module_name: str
    software_version: str
    timestamp: int
    user_id: str
    def __init__(self, markup: _Optional[str] = ..., event_id: _Optional[str] = ..., chunk_num: _Optional[int] = ..., timestamp: _Optional[int] = ..., user_id: _Optional[str] = ..., software_version: _Optional[str] = ..., module_name: _Optional[str] = ...) -> None: ...

class CloudTTSResponse(_message.Message):
    __slots__ = ["audio", "chunk_num", "event_id", "marks", "module_name", "request_source", "software_version", "synthesis_time", "timestamp", "total_time"]
    AUDIO_FIELD_NUMBER: _ClassVar[int]
    CHUNK_NUM_FIELD_NUMBER: _ClassVar[int]
    EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    MARKS_FIELD_NUMBER: _ClassVar[int]
    MODULE_NAME_FIELD_NUMBER: _ClassVar[int]
    REQUEST_SOURCE_FIELD_NUMBER: _ClassVar[int]
    SOFTWARE_VERSION_FIELD_NUMBER: _ClassVar[int]
    SYNTHESIS_TIME_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    TOTAL_TIME_FIELD_NUMBER: _ClassVar[int]
    audio: AudioBuffer
    chunk_num: int
    event_id: str
    marks: _containers.RepeatedCompositeFieldContainer[TTSMark]
    module_name: str
    request_source: RequestSourceType
    software_version: str
    synthesis_time: int
    timestamp: int
    total_time: int
    def __init__(self, request_source: _Optional[_Union[RequestSourceType, str]] = ..., audio: _Optional[_Union[AudioBuffer, _Mapping]] = ..., marks: _Optional[_Iterable[_Union[TTSMark, _Mapping]]] = ..., event_id: _Optional[str] = ..., chunk_num: _Optional[int] = ..., timestamp: _Optional[int] = ..., total_time: _Optional[int] = ..., synthesis_time: _Optional[int] = ..., software_version: _Optional[str] = ..., module_name: _Optional[str] = ...) -> None: ...

class CloudTTSSupplement(_message.Message):
    __slots__ = ["automarkup_time", "chunk_num", "event_id", "markup", "module_name", "software_version", "synthesis_time", "text", "timestamp", "total_time", "translation_time", "tts_engine"]
    AUTOMARKUP_TIME_FIELD_NUMBER: _ClassVar[int]
    CHUNK_NUM_FIELD_NUMBER: _ClassVar[int]
    EVENT_ID_FIELD_NUMBER: _ClassVar[int]
    MARKUP_FIELD_NUMBER: _ClassVar[int]
    MODULE_NAME_FIELD_NUMBER: _ClassVar[int]
    SOFTWARE_VERSION_FIELD_NUMBER: _ClassVar[int]
    SYNTHESIS_TIME_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    TOTAL_TIME_FIELD_NUMBER: _ClassVar[int]
    TRANSLATION_TIME_FIELD_NUMBER: _ClassVar[int]
    TTS_ENGINE_FIELD_NUMBER: _ClassVar[int]
    automarkup_time: int
    chunk_num: int
    event_id: str
    markup: str
    module_name: str
    software_version: str
    synthesis_time: int
    text: str
    timestamp: int
    total_time: int
    translation_time: int
    tts_engine: str
    def __init__(self, timestamp: _Optional[int] = ..., event_id: _Optional[str] = ..., chunk_num: _Optional[int] = ..., text: _Optional[str] = ..., markup: _Optional[str] = ..., translation_time: _Optional[int] = ..., automarkup_time: _Optional[int] = ..., synthesis_time: _Optional[int] = ..., tts_engine: _Optional[str] = ..., total_time: _Optional[int] = ..., software_version: _Optional[str] = ..., module_name: _Optional[str] = ...) -> None: ...

class TTSMark(_message.Message):
    __slots__ = ["end", "start", "time", "type", "value"]
    END_FIELD_NUMBER: _ClassVar[int]
    START_FIELD_NUMBER: _ClassVar[int]
    TIME_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    end: int
    start: int
    time: int
    type: str
    value: str
    def __init__(self, time: _Optional[int] = ..., start: _Optional[int] = ..., end: _Optional[int] = ..., type: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...

class RequestSourceType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = []
