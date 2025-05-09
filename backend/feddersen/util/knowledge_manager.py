import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
from feddersen.models import ExtraMetadata, ItemMetadata
from feddersen.config import EXTRA_MIDDLEWARE_METADATA_KEY
from pydantic import BaseModel, ConfigDict


## Copies from openwebui, otherwise we have to install the whole openwebui requirements
class KnowledgeModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str

    name: str
    description: str

    data: Optional[dict] = None
    meta: Optional[dict] = None

    access_control: Optional[dict] = None

    created_at: int  # timestamp in epoch
    updated_at: int  # timestamp in epoch


class FileMetadataResponse(BaseModel):
    id: str
    meta: dict
    created_at: int  # timestamp in epoch
    updated_at: int  # timestamp in epoch


class KnowledgeResponse(KnowledgeModel):
    files: Optional[list[FileMetadataResponse | dict]] = None


class KnowledgeManager:
    """
    A class for interacting with the knowledge store API.
    Uses aiohttp for asynchronous HTTP requests.
    """

    def __init__(self, base_url: str = None, api_key: str = None, batch_size: int = 5):
        """
        Initialize the KnowledgeManager with base URL and API key.

        Parameters:
        -----------
        base_url : str, optional
            The base URL for the API. If not provided, reads from OPEN_WEBUI_URL env var.
        api_key : str, optional
            The API key for authentication. If not provided, reads from OPEN_WEBUI_API_KEY env var.
        batch_size : int, optional
            The number of files to process in a single batch. Defaults to 5.
        """
        self.base_url = base_url or os.environ.get(
            "OPEN_WEBUI_URL", "http://localhost:8080"
        )
        if not self.base_url.endswith("/api/v1"):
            self.base_url = self.base_url.strip("/") + "/api/v1"
        self.api_key = api_key or os.environ.get("OPEN_WEBUI_API_KEY")

        self.batch_size = batch_size
        self.logger = logging.getLogger(__name__)

        if not self.api_key:
            raise ValueError(
                "API key is required. Provide it as parameter or set OPEN_WEBUI_API_KEY environment variable."
            )

        self.headers = {"Authorization": f"Bearer {self.api_key}"}
        self.session = None

    async def __aenter__(self):
        """Set up the client session when used as a context manager."""
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Close the client session when context manager exits."""
        if self.session:
            await self.session.close()
            self.session = None

    async def _get_session(self):
        """Get the current session or create a new one if needed."""
        if self.session is None:
            return aiohttp.ClientSession()
        return self.session

    async def create_knowledge_if_not_exists(
        self,
        name: str,
        description: str,
        data: Dict[str, Any] = None,
        access_control: Dict[str, Any] = None,
    ) -> Optional[str]:
        """
        Create a knowledge base if it doesn't exist already.

        Parameters:
        -----------
        name : str
            Name of the knowledge base
        description : str
            Description of the knowledge base
        data : Dict, optional
            Additional data for the knowledge base
        access_control : Dict, optional
            Access control settings

        Returns:
        --------
        str or None
            Knowledge base ID if created successfully, else None
        """
        # First check if knowledge base with this name exists
        session = await self._get_session()
        async with session.get(
            f"{self.base_url}/knowledge/", headers=self.headers
        ) as response:
            if response.status == 200:
                knowledge_bases = await response.json()
                for kb in knowledge_bases:
                    if kb.get("name") == name:
                        return kb.get("id")

        # If not found, create it
        data = {
            "name": name,
            "description": description,
            "data": data or {},
            "access_control": access_control or {},
        }

        session = await self._get_session()
        async with session.post(
            f"{self.base_url}/knowledge/create", json=data, headers=self.headers
        ) as response:
            if response.status != 200:
                response_text = await response.text()
                self.logger.error(
                    f"Failed to create knowledge base, response: {response_text}"
                )
                return None

            result = await response.json()
            knowledge_base_id = result.get("id")
            if not knowledge_base_id:
                self.logger.error(
                    f"Failed to get knowledge base ID from response: {result}"
                )
                return None

            return knowledge_base_id

    async def add_files(
        self,
        knowledge_id: str,
        file_paths: List[str],
        metadata: List[ExtraMetadata] = None,
    ) -> List[Dict]:
        """
        Upload files in batches and add them to a knowledge base.

        Parameters:
        -----------
        knowledge_id : str
            ID of the knowledge base
        file_paths : List[str]
            List of paths to files to upload
        metadata : Union[ExtraMetadata, List[ExtraMetadata]], optional
            Metadata for the files. If a single ExtraMetadata object is provided, it will be used for all files.
            If a list, it should match the length of file_paths.

        Returns:
        --------
        List[Dict]
            Results for each file upload operation
        """

        # Process files in batches
        results = []

        # Remove files that are already in the knowledge base. THE CHECK IS BASED ON THE itemURL!
        knowledge_files = await self._retrieve_files_in_knowledge(knowledge_id)
        existing_files = []
        for file in knowledge_files:
            meta_data = file.meta
            if meta_data and meta_data.get(EXTRA_MIDDLEWARE_METADATA_KEY):
                extra_meta = meta_data.get(EXTRA_MIDDLEWARE_METADATA_KEY)
                if extra_meta:
                    item_meta = ExtraMetadata.model_validate(extra_meta)
                    existing_files.append(item_meta.metadata.url)
        existing_files = set(existing_files)

        file_paths_to_upload = []
        metadata_to_upload = []
        if isinstance(metadata, ExtraMetadata):
            metadata = [metadata] * len(file_paths)

        for file_path, meta_data in zip(file_paths, metadata):
            item_meta = meta_data.metadata
            if item_meta and item_meta.url and item_meta.url not in existing_files:
                file_paths_to_upload.append(file_path)
                metadata_to_upload.append(meta_data)
            else:
                self.logger.warning(
                    f"File already exists in knowledge base: {file_path}, skipping."
                )

        for i in range(0, len(file_paths_to_upload), self.batch_size):
            batch_files = file_paths_to_upload[i : i + self.batch_size]
            batch_metadata = metadata_to_upload[i : i + self.batch_size]

            # Upload files in this batch
            batch_file_ids = await self._upload_files_batch(batch_files, batch_metadata)

            # Add the files to the knowledge base using the batch endpoint
            if batch_file_ids:
                batch_result = await self._add_files_to_kb_batch(
                    knowledge_id, batch_file_ids
                )
                results.extend(batch_result)

        return results

    async def remove_files(self, knowledge_id: str, file_ids: List[str]) -> List[Dict]:
        """
        Remove files from a knowledge base.

        Parameters:
        -----------
        knowledge_id : str
            ID of the knowledge base
        file_ids : List[str]
            List of file IDs to remove

        Returns:
        --------
        List[Dict]
            Results for each file removal operation
        """
        results = []
        session = await self._get_session()

        for file_id in file_ids:
            # Remove the file from the knowledge base. This also deletes it from vector db
            # and file db. However, it does not actually remove the file in the file system.
            async with session.post(
                f"{self.base_url}/knowledge/{knowledge_id}/file/remove",
                json={"file_id": file_id},
                headers=self.headers,
            ) as response:
                if response.status != 200:
                    response_text = await response.text()
                    self.logger.error(
                        f"Failed to remove file ID {file_id}, response: {response_text}"
                    )
                    results.append(
                        {"file_id": file_id, "success": False, "error": response_text}
                    )
                else:
                    result = await response.json()
                    results.append(
                        {"file_id": file_id, "success": True, "result": result}
                    )

        return results

    @staticmethod
    def _get_item_url_of_file(file_response: FileMetadataResponse) -> str:
        """
        Get the item URL of a file from its metadata response.

        Parameters:
        -----------
        file_response : FileMetadataResponse
            The file metadata response

        Returns:
        --------
        str
            The item URL of the file
        """
        if file_response and file_response.meta:
            extra_meta = file_response.meta.get(EXTRA_MIDDLEWARE_METADATA_KEY)
            if extra_meta:
                extra_meta = ExtraMetadata.model_validate(extra_meta)
                if url := extra_meta.metadata.url:
                    return url
        return None

    def _get_file_meta_if_exists_in_knowledge(
        self, knowledge_files: List[FileMetadataResponse], file_metadata: ItemMetadata
    ) -> Optional[FileMetadataResponse]:
        """
        Check if a file exists in the knowledge base.

        Parameters:
        -----------
        knowledge_files : KnowledgeResponse
            The knowledge base response containing files
        file_metadata : ExtraMetadata
            Metadata of the file to check

        Returns:
        --------
        Optional[str]
            The file ID if the file exists, else None
        """
        for kf in knowledge_files:
            kf_file_url = self._get_item_url_of_file(kf)
            if kf_file_url and kf_file_url == file_metadata.url:
                return kf
        return None

    async def update_files(
        self,
        knowledge_id: str,
        file_paths: List[str],
        metadata: List[ExtraMetadata],
    ) -> List[Dict]:
        """
        Update files in a knowledge base by removing and adding them again.

        Parameters:
        -----------
        knowledge_id : str
            ID of the knowledge base
        file_paths : List[str]
            List of paths to files to update
        file_names : List[str], optional
            List of file names to match in the knowledge base.
            If not provided, uses the basename of the file_paths.
        metadata : List[ExtraMetadata], optional
            Metadata for the files.

        Returns:
        --------
        List[Dict]
            Results for each file update operation
        """
        results = []

        # Get all files in knowledge base
        knowledge_files = await self._retrieve_files_in_knowledge(knowledge_id)

        for i, (file_path, file_metadata) in enumerate(zip(file_paths, metadata)):
            # Find file ID by name
            file = self._get_file_meta_if_exists_in_knowledge(
                knowledge_files, file_metadata.metadata
            )
            file_id = file.id if file else None
            file_item_url = self._get_item_url_of_file(file)

            if not file_id:
                self.logger.warning(f"File not found: {file_item_url}")
                results.append(
                    {
                        "file_name": file_item_url,
                        "success": False,
                        "error": "File not found",
                    }
                )
                continue

            # Remove the file from the knowledge base
            remove_result = await self.remove_files(knowledge_id, [file_id])

            if remove_result[0].get("success", False):
                add_result = await self._upload_file_and_add_to_kb(
                    file_path, file_metadata, knowledge_id
                )
                results.append(
                    {"file_name": file_item_url, "success": True, "result": add_result}
                )
            else:
                results.append(
                    {
                        "file_name": file_item_url,
                        "success": False,
                        "error": "Failed to remove file",
                    }
                )

        return results

    async def delete_knowledge(self, knowledge_id: str) -> Dict:
        """
        Delete a knowledge base.

        Parameters:
        -----------
        knowledge_id : str
            ID of the knowledge base to delete

        Returns:
        --------
        Dict
            Result of the delete operation
        """
        session = await self._get_session()
        async with session.delete(
            f"{self.base_url}/knowledge/{knowledge_id}/delete",
            headers=self.headers,
        ) as response:
            if response.status != 200:
                response_text = await response.text()
                self.logger.error(
                    f"Failed to delete knowledge base {knowledge_id}, response: {response_text}"
                )
                return {"success": False, "error": response_text}

            result = await response.json()
            return {"success": True, "result": result}

    async def _upload_files_batch(
        self, file_paths: List[str], metadata: List[ExtraMetadata] = None
    ) -> List[str]:
        """
        Upload a batch of files in parallel.

        Parameters:
        -----------
        file_paths : List[str]
            List of paths to files to upload
        metadata : List[ExtraMetadata], optional
            Metadata for the files

        Returns:
        --------
        List[str]
            List of file IDs for successfully uploaded files
        """
        # Upload files in parallel
        upload_tasks = []

        for i, file_path in enumerate(file_paths):
            task = self._send_file(
                f"{self.base_url}/files/", file_path, metadata[i] if metadata else None
            )
            upload_tasks.append(task)

        # Wait for all uploads to complete
        file_ids = await asyncio.gather(*upload_tasks)

        # Filter out any failed uploads (None values)
        return [file_id for file_id in file_ids if file_id]

    async def _add_files_to_kb_batch(
        self, knowledge_id: str, file_ids: List[str]
    ) -> List[Dict]:
        """
        Add multiple files to a knowledge base using the batch endpoint.

        Parameters:
        -----------
        knowledge_id : str
            ID of the knowledge base
        file_ids : List[str]
            List of file IDs to add

        Returns:
        --------
        List[Dict]
            Results for each file operation
        """
        results = []

        if not file_ids:
            return results

        # Use the batch endpoint to add all files at once
        session = await self._get_session()
        async with session.post(
            f"{self.base_url}/knowledge/{knowledge_id}/files/batch/add",
            json=[{"file_id": file_id} for file_id in file_ids],
            headers=self.headers,
        ) as response:
            if response.status != 200:
                response_text = await response.text()
                self.logger.error(
                    f"Failed to batch add files to knowledge base, response: {response_text}"
                )
                # Create error results for each file
                for file_id in file_ids:
                    results.append(
                        {
                            "file_id": file_id,
                            "success": False,
                            "error": f"Batch operation failed: {response_text}",
                        }
                    )
            else:
                batch_result = await response.json()
                # Create results for each file based on the batch response
                for file_id in file_ids:
                    results.append(
                        {
                            "success": True,
                            "result": {"id": file_id, "batch_result": batch_result},
                        }
                    )

        return results

    async def _upload_file_and_add_to_kb(
        self, file_path: str, metadata: ExtraMetadata, knowledge_id: str
    ) -> Dict:
        """
        Upload a file and add it to a knowledge base.

        Parameters:
        -----------
        file_path : str
            Path to the file to upload
        metadata : metadata
            Metadata for the file
        knowledge_id : str
            ID of the knowledge base to add the file to

        Returns:
        --------
        Dict
            Result of the operation
        """
        # Upload the file
        file_id = await self._send_file(f"{self.base_url}/files/", file_path, metadata)
        if not file_id:
            return {"success": False, "error": "Failed to upload file"}

        # Add the file to the knowledge base
        session = await self._get_session()
        async with session.post(
            f"{self.base_url}/knowledge/{knowledge_id}/file/add",
            json={"file_id": file_id},
            headers=self.headers,
        ) as response:
            if response.status != 200:
                response_text = await response.text()
                self.logger.error(
                    f"Failed to add file to knowledge base: {file_path}, response: {response_text}"
                )
                return {"success": False, "error": response_text}

            result = await response.json()
            return {"success": True, "result": result}

    async def _send_file(
        self, url: str, file_path: str, metadata: ExtraMetadata
    ) -> Optional[str]:
        """
        Upload a file to the API.

        Parameters:
        -----------
        url : str
            URL to upload the file to
        file_path : str
            Path to the file to upload
        metadata : ExtraMetadata
            Metadata for the file

        Returns:
        --------
        str or None
            File ID if uploaded successfully, else None
        """
        file_path = Path(file_path)
        if not file_path.exists():
            self.logger.error(f"File not found: {file_path}")
            return None

        # Prepare the form data with the file and metadata
        with open(file_path, "rb") as f:
            form_data = aiohttp.FormData()
            form_data.add_field(
                "file",
                f.read(),
                filename=file_path.name,
                content_type="application/octet-stream",
            )
            if metadata is not None:
                form_data.add_field("file_metadata", metadata.model_dump_json())

            session = await self._get_session()
            async with session.post(
                url, data=form_data, headers=self.headers
            ) as response:
                if response.status != 200:
                    response_text = await response.text()
                    self.logger.error(
                        f"Failed to upload file: {file_path}, response: {response_text}"
                    )
                    return None

                result = await response.json()
                file_id = result.get("id")
                if not file_id:
                    self.logger.error(f"Failed to get file ID for: {file_path}")
                    return None

                return file_id

    async def _retrieve_files_in_knowledge(
        self, knowledge_id: str
    ) -> List[FileMetadataResponse]:
        """
        Retrieve all files in a knowledge base.

        Parameters:
        -----------
        knowledge_id : str
            ID of the knowledge base

        Returns:
        --------
        List[Dict]
            List of files in the knowledge base
        """
        session = await self._get_session()
        async with session.get(
            f"{self.base_url}/knowledge/{knowledge_id}", headers=self.headers
        ) as response:
            if response.status != 200:
                response_text = await response.text()
                self.logger.error(
                    f"Failed to get knowledge base files, response: {response_text}"
                )
                return []

            knowledge_data = await response.json()
            knowledge_data = KnowledgeResponse.model_validate(knowledge_data)
            return knowledge_data.files

    async def query_knowledge(
        self,
        knowledge_ids: list[str],
        query: str,
        top_k: int = 5,
        as_user: bool = False,
    ) -> Dict:
        """
        Query a knowledge base.

        Parameters:
        -----------
        knowledge_id : list[str]
            ID of the knowledge bases
        query : str
            Query string
        top_k : int, optional
            Number of top results to return (default is 5)
        filter : Dict, optional
            Filter for the query
        as_user : bool, optional
            If True, use the user API key for authentication (of the user with only role 'test', not admin)

        Returns:
        --------
        Dict
            Query results
        """
        session = await self._get_session()

        headers = self.headers.copy()
        if as_user and os.getenv("OPEN_WEBUI_TEST_USER_API_KEY"):
            headers = {
                "Authorization": f"Bearer {os.getenv('OPEN_WEBUI_TEST_USER_API_KEY')}"
            }

        async with session.post(
            f"{self.base_url}/retrieval/query/collection",
            json={
                "collection_names": knowledge_ids,
                "query": query,
                "k": top_k,
            },
            headers=headers,
        ) as response:
            if response.status != 200:
                response_text = await response.text()
                self.logger.error(
                    f"Failed to query knowledge base, response: {response_text}"
                )
                return []

            response = await response.json()
            return response
