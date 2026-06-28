# service/storage.py
"""Работа с MinIO (S3-совместимое объектное хранилище)."""
import aioboto3
from botocore.exceptions import ClientError
from config import settings

_session = None


def get_session():
    global _session
    if _session is None:
        _session = aioboto3.Session()
    return _session


def s3_client():
    return get_session().client(
        "s3",
        endpoint_url=f"http://{settings.minio_endpoint}",
        aws_access_key_id=settings.minio_root_user,
        aws_secret_access_key=settings.minio_root_password,
    )


async def ensure_bucket():
    async with s3_client() as s3:
        try:
            await s3.head_bucket(Bucket=settings.minio_bucket)
        except ClientError:
            await s3.create_bucket(Bucket=settings.minio_bucket)


async def upload_file(key: str, data: bytes, content_type: str = "application/octet-stream"):
    async with s3_client() as s3:
        await s3.put_object(
            Bucket=settings.minio_bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )


async def download_file(key: str) -> bytes:
    async with s3_client() as s3:
        response = await s3.get_object(Bucket=settings.minio_bucket, Key=key)
        return await response["Body"].read()


async def delete_file(key: str):
    async with s3_client() as s3:
        await s3.delete_object(Bucket=settings.minio_bucket, Key=key)
