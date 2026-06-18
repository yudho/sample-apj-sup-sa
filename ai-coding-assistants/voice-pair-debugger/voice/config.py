import os

import boto3
from dotenv import load_dotenv

load_dotenv()

DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
AWS_PROFILE = os.environ.get("AWS_PROFILE", None)

BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
DEEPGRAM_TTS_VOICE = os.environ.get("DEEPGRAM_TTS_VOICE", "aura-2-draco-en")

def get_boto3_session() -> boto3.Session:
    """Create a boto3 session using the configured profile and region."""
    return boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
