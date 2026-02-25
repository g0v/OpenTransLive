from pymongo import AsyncMongoClient
import os
from datetime import datetime, timezone
from .config import MONGODB_SETTINGS

client = AsyncMongoClient(host=MONGODB_SETTINGS.get('host', 'mongodb'), port=MONGODB_SETTINGS.get('port', 27017))
db = client[MONGODB_SETTINGS.get('db', 'opentranslive-db')]

rooms_collection = db['room']
transcription_store_collection = db['transcription_store']
