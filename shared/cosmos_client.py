"""
Shared async Cosmos DB client.
Used by: prompt-registry, eval-harness, inference (cost records).

Falls back to an in-memory dict when COSMOS_CONN_STR is not set,
so all services work locally without Azure credentials.
"""
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger("cosmos_client")


class CosmosClient:
    def __init__(self, container_name: str, database_name: str = "llm-platform"):
        self.container_name = container_name
        self.database_name = database_name
        self._real_client = None
        self._container = None
        self._memory: Dict[str, Any] = {}

        conn_str = os.getenv("COSMOS_CONN_STR", "")
        if conn_str:
            try:
                from azure.cosmos.aio import CosmosClient as AzureCosmosClient
                self._real_client = AzureCosmosClient.from_connection_string(conn_str)
                logger.info("Cosmos DB connected (container=%s)", container_name)
            except ImportError:
                logger.warning("azure-cosmos not installed — using in-memory store")
        else:
            logger.warning("COSMOS_CONN_STR not set — using in-memory store")

    async def _get_container(self):
        if self._container is not None:
            return self._container
        db = self._real_client.get_database_client(self.database_name)
        self._container = db.get_container_client(self.container_name)
        return self._container

    async def upsert(self, document: dict) -> dict:
        if "id" not in document:
            raise ValueError("document must have an 'id' field")
        if self._real_client:
            container = await self._get_container()
            return await container.upsert_item(document)
        self._memory[document["id"]] = document
        return document

    async def get(self, doc_id: str, partition_key: Optional[str] = None) -> Optional[dict]:
        if self._real_client:
            container = await self._get_container()
            try:
                return await container.read_item(item=doc_id, partition_key=partition_key or doc_id)
            except Exception:
                return None
        return self._memory.get(doc_id)

    async def query(self, query: str, parameters: Optional[List[dict]] = None) -> List[dict]:
        if self._real_client:
            container = await self._get_container()
            items = []
            async for item in container.query_items(query=query, parameters=parameters or []):
                items.append(item)
            return items
        return list(self._memory.values())

    async def delete(self, doc_id: str, partition_key: Optional[str] = None) -> bool:
        if self._real_client:
            container = await self._get_container()
            try:
                await container.delete_item(item=doc_id, partition_key=partition_key or doc_id)
                return True
            except Exception:
                return False
        return self._memory.pop(doc_id, None) is not None

    async def close(self):
        if self._real_client:
            await self._real_client.close()