from mongoengine import connect, Document, StringField, DictField, DateTimeField, ListField, FloatField
import os
from datetime import datetime, timezone
from typing import ClassVar, Any
from .config import MONGODB_SETTINGS

connect(**MONGODB_SETTINGS)

class Room(Document):
    """Room model for storing session information"""
    # Type hint for MongoEngine's objects manager
    objects: ClassVar[Any]
    
    sid = StringField(required=True, unique=True)
    secret_key = StringField(required=True)
    extra = DictField(default=dict)
    created_at = DateTimeField(default=datetime.now(timezone.utc))

class TranscriptionStore(Document):
    """Model for storing transcription data matching the JSON structure"""
    objects: ClassVar[Any]

    sid = StringField(required=True, unique=True)
    transcriptions = ListField(DictField(), default=list)
    stream_start_time = FloatField()
    updated_at = DateTimeField(default=datetime.now(timezone.utc))

