from pymongo import AsyncMongoClient, ASCENDING, DESCENDING
import os
from datetime import datetime, timezone
from .config import MONGODB_SETTINGS

client = AsyncMongoClient(host=MONGODB_SETTINGS.get('host', 'mongodb'), port=MONGODB_SETTINGS.get('port', 27017), tz_aware=True)
db = client[MONGODB_SETTINGS.get('db', 'opentranslive-db')]

rooms_collection = db['room']
transcription_store_collection = db['transcription_store']
users_collection = db['users']


async def init_indexes():
    await rooms_collection.create_index([("sid", ASCENDING)], unique=True)
    await transcription_store_collection.create_index([("sid", ASCENDING), ("created_at", DESCENDING)])
    await users_collection.create_index([("email", ASCENDING)], unique=True)
    await users_collection.create_index([("user_uid", ASCENDING)])
