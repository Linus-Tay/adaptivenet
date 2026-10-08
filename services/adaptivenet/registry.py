"""Operator-only registration. Saving an endpoint does not probe or authorize it.

SQLite is the local prototype store. Retain these API contracts when replacing
the repository with PostgreSQL and authenticated tenant ownership.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from typing import Literal
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator
from .gateway import authorize

router = APIRouter(prefix="/applications", tags=["Application registry"])

class Endpoint(BaseModel):
    endpoint_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,47}$")
    region: str = Field(min_length=2, max_length=40, pattern=r"^[a-z0-9-]+$")
    url: HttpUrl

    @field_validator("url")
    @classmethod
    def no_secrets_in_url(cls, url):
        if url.username or url.password or url.query or url.fragment:
            raise ValueError("Use an endpoint URL without credentials, query or fragment")
        return url

class ApplicationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    mode: Literal["local", "aws"] = "local"
    endpoints: list[Endpoint] = Field(min_length=1, max_length=10)

    @field_validator("name")
    @classmethod
    def nonblank(cls, name):
        if not name.strip():
            raise ValueError("Name cannot be blank")
        return name.strip()

    @model_validator(mode="after")
    def unique_endpoints(self):
        ids = [endpoint.endpoint_id for endpoint in self.endpoints]
        if len(ids) != len(set(ids)):
            raise ValueError("Endpoint IDs must be unique within an application")
        if self.mode == "aws" and any(endpoint.url.scheme != "https" for endpoint in self.endpoints):
            raise ValueError("AWS endpoint URLs must use HTTPS")
        return self

class Registry:
    def __init__(self, path: Path):
        self.path = path

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("CREATE TABLE IF NOT EXISTS applications (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        return connection

    def create(self, body: ApplicationCreate):
        record = {"id": str(uuid4()), "schema_version": 1, **body.model_dump(mode="json"),
                  "created_at": datetime.now(timezone.utc).isoformat(),
                  "connections": {"internal_telemetry": "not_connected", "external_telemetry": "not_connected",
                                  "routing": "not_connected", "scaling": "not_connected"},
                  "automation": "disabled", "registration_status": "registered_unverified"}
        connection = self.connect()
        try:
            with connection:
                connection.execute("INSERT INTO applications VALUES (?, ?)", (record["id"], json.dumps(record)))
        finally:
            connection.close()
        return record

    def list(self):
        connection = self.connect()
        try:
            return [json.loads(row[0]) for row in connection.execute("SELECT payload FROM applications ORDER BY rowid DESC")]
        finally:
            connection.close()

def repository():
    return Registry(Path(os.getenv("REGISTRY_DB_PATH", "data/registry.db")))

@router.get("")
def applications(x_lab_token: str | None = Header(default=None)):
    authorize(x_lab_token)
    return {"items": repository().list()}

@router.post("", status_code=201)
def register(body: ApplicationCreate, x_lab_token: str | None = Header(default=None)):
    authorize(x_lab_token)
    return repository().create(body)

@router.get("/{application_id}")
def application(application_id: str, x_lab_token: str | None = Header(default=None)):
    authorize(x_lab_token)
    for record in repository().list():
        if record["id"] == application_id:
            return record
    raise HTTPException(404, "Application not found")
