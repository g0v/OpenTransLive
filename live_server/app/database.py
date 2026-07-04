from pymongo import AsyncMongoClient, ASCENDING, DESCENDING
from .config import MONGODB_SETTINGS

client = AsyncMongoClient(host=MONGODB_SETTINGS.get('host', 'mongodb'), port=MONGODB_SETTINGS.get('port', 27017), tz_aware=True)
db = client[MONGODB_SETTINGS.get('db', 'opentranslive-db')]

rooms_collection = db['room']
transcription_store_collection = db['transcription_store']
transcription_segments_collection = db['transcription_segments']
users_collection = db['users']


async def init_indexes():
    await rooms_collection.create_index([("sid", ASCENDING)], unique=True)
    await rooms_collection.create_index([("admin_email", ASCENDING)])
    await transcription_store_collection.create_index([("sid", ASCENDING), ("created_at", DESCENDING)])
    await transcription_segments_collection.create_index([("sid", ASCENDING), ("start_time", ASCENDING)])
    await users_collection.create_index([("email", ASCENDING)], unique=True)
    await users_collection.create_index([("user_uid", ASCENDING)])
    # Sparse+unique: only users that have generated a key carry the field, and no
    # two users can ever share a hash. Lookups on every API-key request hit this.
    await users_collection.create_index([("api_key_hash", ASCENDING)], unique=True, sparse=True)
