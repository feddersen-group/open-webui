import asyncio
import logging
import os
import tempfile
from pathlib import Path

import pytest
from dotenv import load_dotenv

from feddersen.models import ExtraMetadata, ItemMetadata, ItemPermissions
from feddersen.util.knowledge_manager import KnowledgeManager

# Test fixtures and helper functions


@pytest.fixture
def test_files():
    """Create test files for testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create a text file
        text_file = Path(temp_dir) / "test_document.md"
        with open(text_file, "w") as f:
            f.write("This is a test document for the knowledge store integration test.")

        # Use the real PDF file from test files directory
        pdf_file = Path(__file__).parent.parent / "test_files/rothschild_giraffe.pdf"
        if not pdf_file.exists():
            pytest.skip(f"PDF test file not found: {pdf_file}")

        yield {
            "text_file": str(text_file),
            "pdf_file": str(pdf_file),
        }


@pytest.fixture
def test_metadata():
    """Create test metadata matching the ExtraMetadata model."""
    return ExtraMetadata(
        auth=ItemPermissions(users=["user1", "user2"], groups=["group1"]),
        metadata=ItemMetadata(
            title="Test Document",
            url="https://example.com/test",
            context_url="https://example.com/context",
            date="2023-07-01T12:00:00Z",
            source="integration_test",
        ),
    )


@pytest.fixture
async def knowledge_manager():
    """Create a KnowledgeManager instance."""
    # Using environment variables for BASE_URL and API_KEY

    if not os.getenv("OPEN_WEBUI_URL") and not os.getenv("OPEN_WEBUI_API_KEY"):
        load_dotenv(Path(__file__).parent.parent / ".env.test")

    # Set a small batch size for testing
    manager = KnowledgeManager(batch_size=3)
    return manager


@pytest.fixture
async def knowledge_base_id(knowledge_manager):
    """Create a knowledge base for testing and clean it up after tests."""
    print("\n--- Starting knowledge_base_id fixture ---")

    # Create a unique name for the knowledge base to avoid conflicts in parallel tests
    kb_name = f"test-kb-{os.urandom(4).hex()}"
    kb_description = "Knowledge base for integration testing"

    print(f"Creating knowledge base with name: {kb_name}")

    async with knowledge_manager:
        # Create the knowledge base
        kb_id = await knowledge_manager.create_knowledge_if_not_exists(
            name=kb_name, description=kb_description
        )

    print(f"Created knowledge base with ID: {kb_id}")

    assert kb_id is not None, "Failed to create knowledge base"

    yield kb_id

    # Clean up - delete the knowledge base after tests
    print(f"Cleaning up knowledge base with ID: {kb_id}")

    async with knowledge_manager:
        result = await knowledge_manager.delete_knowledge(kb_id)
    assert result["success"], f"Failed to delete knowledge base: {result.get('error')}"
    print("--- Finished knowledge_base_id fixture ---")


@pytest.mark.integration
class TestKnowledgeManager:
    # Tests
    @pytest.mark.asyncio
    async def test_create_knowledge_base(self, knowledge_manager):
        """Test creating a knowledge base."""
        kb_name = f"test-kb-create-{os.urandom(4).hex()}"

        async with knowledge_manager:
            kb_id = await knowledge_manager.create_knowledge_if_not_exists(
                name=kb_name, description="Test knowledge base creation"
            )

            assert kb_id is not None, "Failed to create knowledge base"

            # Clean up
            result = await knowledge_manager.delete_knowledge(kb_id)
            assert result["success"], "Failed to delete knowledge base"

    @pytest.mark.asyncio
    async def test_delete_knowledge_base(self, knowledge_manager):
        """Test deleting a knowledge base."""
        # Create a temporary knowledge base for this test
        kb_name = f"test-kb-delete-{os.urandom(4).hex()}"

        async with knowledge_manager:
            kb_id = await knowledge_manager.create_knowledge_if_not_exists(
                name=kb_name, description="Test knowledge base deletion"
            )

            assert kb_id is not None, "Failed to create knowledge base"

            # Delete the knowledge base
            result = await knowledge_manager.delete_knowledge(kb_id)
            assert result[
                "success"
            ], f"Failed to delete knowledge base: {result.get('error')}"

            # Verify the knowledge base is deleted by trying to retrieve its files
            # This should fail or return empty results
            files = await knowledge_manager._retrieve_files_in_knowledge(kb_id)
            assert len(files) == 0, "Knowledge base still exists after deletion"

    @pytest.mark.asyncio
    async def test_add_files(
        self, knowledge_manager, knowledge_base_id, test_files, test_metadata
    ):
        async with knowledge_manager:
            """Test adding files to a knowledge base."""
            results = await knowledge_manager.add_files(
                knowledge_id=knowledge_base_id,
                file_paths=[test_files["text_file"]],
                metadata=[test_metadata],
            )

        assert len(results) == 1, "Expected one result for one file"
        assert results[0]["success"], f"Failed to add file: {results[0].get('error')}"

    @pytest.mark.asyncio
    async def test_add_files_only_once(
        self, knowledge_manager, knowledge_base_id, test_files, test_metadata, caplog
    ):
        async with knowledge_manager:
            """Test adding files to a knowledge base."""
            results = await knowledge_manager.add_files(
                knowledge_id=knowledge_base_id,
                file_paths=[test_files["text_file"]],
                metadata=[test_metadata],
            )

            assert len(results) == 1, "Expected one result for one file"
            assert results[0][
                "success"
            ], f"Failed to add file: {results[0].get('error')}"

            with caplog.at_level(logging.WARNING):
                results = await knowledge_manager.add_files(
                    knowledge_id=knowledge_base_id,
                    file_paths=[test_files["text_file"]],
                    metadata=[test_metadata],
                )

                assert caplog.records[-1].levelname == "WARNING"

            assert len(results) == 0, "Expected no result for duplicate file upload"

    @pytest.mark.asyncio
    async def test_add_multiple_files(
        self, knowledge_manager, knowledge_base_id, test_files, test_metadata
    ):
        """Test adding multiple files to a knowledge base."""
        # Create a slightly modified metadata for the second file
        second_metadata = ExtraMetadata(
            auth=ItemPermissions(users=["user3"], groups=["group2"]),
            metadata=ItemMetadata(
                title="PDF Test Document",
                url="https://example.com/pdf",
                date="2023-07-01T14:00:00Z",
                source="integration_test",
            ),
        )

        async with knowledge_manager:
            # Test that files are processed in batches (batch_size=3 in fixture)
            results = await knowledge_manager.add_files(
                knowledge_id=knowledge_base_id,
                file_paths=[test_files["text_file"], test_files["pdf_file"]],
                metadata=[test_metadata, second_metadata],
            )

            assert len(results) == 2, "Expected two results for two files"
            assert all(
                result["success"] for result in results
            ), "Failed to add all files"

            # Verify files were added by retrieving them
            files = await knowledge_manager._retrieve_files_in_knowledge(
                knowledge_base_id
            )
            assert len(files) >= 2, "Expected at least two files in knowledge base"

    @pytest.mark.asyncio
    async def test_batch_processing(
        self, knowledge_manager: KnowledgeManager, knowledge_base_id
    ):
        """Test batch processing of multiple files."""
        # Create several temporary files to test batch processing
        temp_files = []
        temp_metadata = []

        try:
            # Create 7 temporary files (more than the batch size of 3)
            for i in range(7):
                with tempfile.NamedTemporaryFile(
                    suffix=".txt", mode="w", delete=False
                ) as temp:
                    temp.write(f"This is test file {i} for batch processing test.")
                    temp_files.append(temp.name)

                    # Create unique metadata for each file
                    meta = ExtraMetadata(
                        auth=ItemPermissions(users=[f"user{i}"], groups=[f"group{i}"]),
                        metadata=ItemMetadata(
                            title=f"Batch Test Document {i}",
                            url=f"https://example.com/batch/{i}",
                            date=f"2023-07-{i + 1:02d}T12:00:00Z",
                            source="batch_test",
                        ),
                    )
                    temp_metadata.append(meta)

            async with knowledge_manager:

                # Add files which should trigger multiple batches (with batch_size=3)
                results = await knowledge_manager.add_files(
                    knowledge_id=knowledge_base_id,
                    file_paths=temp_files,
                    metadata=temp_metadata,
                )

                # Verify all files were processed
                assert len(results) == 7, "Expected seven results for seven files"
                assert all(
                    result["success"] for result in results
                ), "Failed to add all files in batch"

                # Verify files were added by retrieving them
                files = await knowledge_manager._retrieve_files_in_knowledge(
                    knowledge_base_id
                )
                assert (
                    len(files) >= 7
                ), "Expected at least seven files in knowledge base"

                # Clean up - remove the added files
                remove_results = await knowledge_manager.remove_files(
                    knowledge_id=knowledge_base_id, file_ids=[file.id for file in files]
                )
                assert all(
                    result["success"] for result in remove_results
                ), "Failed to clean up test files"
        finally:
            # Clean up temporary files
            for temp_file in temp_files:
                if os.path.exists(temp_file):
                    os.unlink(temp_file)

    @pytest.mark.asyncio
    async def test_remove_files(
        self, knowledge_manager, knowledge_base_id, test_files, test_metadata
    ):
        """Test removing files from a knowledge base."""
        async with knowledge_manager:
            # First add a file
            add_results = await knowledge_manager.add_files(
                knowledge_id=knowledge_base_id,
                file_paths=[test_files["text_file"]],
                metadata=[test_metadata],
            )

            assert add_results[0]["success"], "Failed to add file for removal test"

            # Get file ID from the result
            file_id = add_results[0]["result"]["id"]

            # Now remove the file
            remove_results = await knowledge_manager.remove_files(
                knowledge_id=knowledge_base_id, file_ids=[file_id]
            )

        assert len(remove_results) == 1, "Expected one result for one file removal"
        assert remove_results[0][
            "success"
        ], f"Failed to remove file: {remove_results[0].get('error')}"

    @pytest.mark.asyncio
    async def test_update_files(
        self, knowledge_manager, knowledge_base_id, test_files, test_metadata
    ):
        """Test updating files in a knowledge base."""
        async with knowledge_manager:
            # First add a file
            add_results = await knowledge_manager.add_files(
                knowledge_id=knowledge_base_id,
                file_paths=[test_files["text_file"]],
                metadata=[test_metadata],
            )

            assert add_results[0]["success"], "Failed to add file for update test"

            # Wait a moment to ensure the file is processed
            await asyncio.sleep(1)

            # Now update the file with new metadata
            updated_metadata = ExtraMetadata(
                auth=ItemPermissions(users=["updated_user"], groups=["updated_group"]),
                metadata=ItemMetadata(
                    title="Updated Test Document",
                    url=test_metadata.metadata.url,
                    date="2023-07-02T12:00:00Z",
                    source="updated_integration_test",
                ),
            )

            update_results = await knowledge_manager.update_files(
                knowledge_id=knowledge_base_id,
                file_paths=[test_files["text_file"]],
                metadata=[updated_metadata],
            )

            assert len(update_results) == 1, "Expected one result for one file update"
            assert update_results[0][
                "success"
            ], f"Failed to update file: {update_results[0].get('error')}"

    @pytest.mark.asyncio
    async def test_retrieve_files_in_knowledge(
        self,
        knowledge_manager: KnowledgeManager,
        knowledge_base_id,
        test_files,
        test_metadata,
    ):
        """Test retrieving files in a knowledge base using the internal method."""
        async with knowledge_manager:
            # First add a file
            add_results = await knowledge_manager.add_files(
                knowledge_id=knowledge_base_id,
                file_paths=[test_files["text_file"]],
                metadata=[test_metadata],
            )

            assert add_results[0]["success"], "Failed to add file for retrieval test"

            # Retrieve files
            files = await knowledge_manager._retrieve_files_in_knowledge(
                knowledge_base_id
            )

            assert len(files) >= 1, "Expected at least one file in knowledge base"

            # Check if the file we added is in the list
            for uploaded_file, received_file in zip(add_results, files):
                assert (
                    uploaded_file["result"]["id"] == received_file.id
                ), "Added file not found in knowledge base"

    @pytest.mark.asyncio
    async def test_full_lifecycle(self, knowledge_manager):
        """Test the full lifecycle of a knowledge base: create, add, update, remove, delete."""
        # Create a knowledge base
        kb_name = f"test-kb-lifecycle-{os.urandom(4).hex()}"

        async with knowledge_manager:
            kb_id = await knowledge_manager.create_knowledge_if_not_exists(
                name=kb_name, description="Test knowledge base lifecycle"
            )

            assert kb_id is not None, "Failed to create knowledge base"

            # Create multiple temporary files to test batch processing in lifecycle
            temp_files = []
            temp_metadata = []

            try:
                # Create 4 temporary files (more than default batch size)
                for i in range(4):
                    with tempfile.NamedTemporaryFile(
                        suffix=".txt", mode="w", delete=False
                    ) as temp:
                        temp.write(
                            f"This is test file {i} for lifecycle batch processing test."
                        )
                        temp_files.append(temp.name)

                        # Create unique metadata for each file
                        meta = ExtraMetadata(
                            auth=ItemPermissions(
                                users=[f"lifecycle_user{i}"],
                                groups=[f"lifecycle_group{i}"],
                            ),
                            metadata=ItemMetadata(
                                title=f"Lifecycle Test {i}",
                                url=f"https://example.com/lifecycle/{i}",
                                date=f"2023-07-{i + 1:02d}T12:00:00Z",
                                source="lifecycle_test",
                            ),
                        )
                        temp_metadata.append(meta)

                # Add files in batch
                add_results = await knowledge_manager.add_files(
                    knowledge_id=kb_id, file_paths=temp_files, metadata=temp_metadata
                )

                assert len(add_results) == 4, "Expected four results for four files"
                assert all(
                    result["success"] for result in add_results
                ), "Failed to add files in lifecycle test"

                # Wait to ensure files are processed
                await asyncio.sleep(1)

                # Update the file "0". The identification is the file url!
                updated_metadata = ExtraMetadata(
                    auth=ItemPermissions(
                        users=["updated_lifecycle_user"],
                        groups=["updated_lifecycle_group"],
                    ),
                    metadata=ItemMetadata(
                        title="Updated Lifecycle Test",
                        url="https://example.com/lifecycle/0",
                        date="2023-07-04T12:00:00Z",
                        source="updated_lifecycle_test",
                    ),
                )

                update_results = await knowledge_manager.update_files(
                    knowledge_id=kb_id,
                    file_paths=[temp_files[0]],
                    metadata=[updated_metadata],
                )

                assert update_results[0][
                    "success"
                ], "Failed to update file in lifecycle test"

                # Retrieve files to get file IDs
                files = await knowledge_manager._retrieve_files_in_knowledge(kb_id)
                assert len(files) >= 4, "Expected at least four files in knowledge base"

                # Remove the files
                for file in files:
                    remove_results = await knowledge_manager.remove_files(
                        knowledge_id=kb_id, file_ids=[file.id]
                    )
                    assert remove_results[0][
                        "success"
                    ], f"Failed to remove file {file.id} in lifecycle test"

                # Delete the knowledge base
                delete_result = await knowledge_manager.delete_knowledge(kb_id)
                assert delete_result[
                    "success"
                ], "Failed to delete knowledge base in lifecycle test"

            finally:
                # Clean up the temporary files
                for temp_file in temp_files:
                    if os.path.exists(temp_file):
                        os.unlink(temp_file)

    @pytest.mark.asyncio
    async def test_query_knowledge(
        self, knowledge_manager: KnowledgeManager, knowledge_base_id: str
    ):
        """Test querying a knowledge base with auth permission filtering."""

        test_user_mail = os.getenv("OPEN_WEBUI_TEST_USER_MAIL")
        if not test_user_mail:
            pytest.fail(
                "Test user not set in environment variables. Cannot test user-specific query. Set OPEN_WEBUI_TEST_USER_MAIL and OPEN_WEBUI_TEST_USER_API_KEY."
            )

        # Create temporary test files with different auth permissions
        temp_files = []
        file_contents = [
            "This document contains information about apples and fruits.",
            "This document is about bananas and tropical fruits.",
            "This document discusses cars and vehicles, not related to fruits.",
            "This document talks about various fruits including oranges and grapes.",
        ]

        # Different auth permissions to test filtering
        permissions = [
            ItemPermissions(users=["user1", "user2"], groups=["group1"]),  # File 1
            ItemPermissions(
                users=["user3", test_user_mail], groups=["group1"]
            ),  # File 2
            ItemPermissions(users=["user1"], groups=["group2"]),  # File 3
            ItemPermissions(users=[test_user_mail], groups=["group3"]),  # File 4
        ]

        metadatas = []

        async with knowledge_manager:
            try:
                # Create test files with different content and permissions
                for i, content in enumerate(file_contents):
                    with tempfile.NamedTemporaryFile(
                        suffix=".txt", mode="w", delete=False
                    ) as temp:
                        temp.write(content)
                        temp_files.append(temp.name)

                        # Create metadata with different auth permissions
                        meta = ExtraMetadata(
                            auth=permissions[i],
                            metadata=ItemMetadata(
                                title=f"Test Document {i}",
                                url=f"https://example.com/doc/{i}",
                                date=f"2023-07-{i+1:02d}T12:00:00Z",
                                source="query_test",
                            ),
                        )
                        metadatas.append(meta)

                # Add files to the knowledge base
                results = await knowledge_manager.add_files(
                    knowledge_id=knowledge_base_id,
                    file_paths=temp_files,
                    metadata=metadatas,
                )

                assert all(
                    result["success"] for result in results
                ), "Failed to add test files for query test"

                # Query the knowledge base for "fruits"
                query_results = await knowledge_manager.query_knowledge(
                    knowledge_ids=[knowledge_base_id],
                    query="What fruits are mentioned?",
                    top_k=5,
                    as_user=False,
                )

                query_results = query_results.get("documents", [])
                # Verify we got results
                assert len(query_results) > 0, "Expected query results but got none"

                documents = query_results[0]
                assert len(documents) == len(
                    file_contents
                ), "Expected four documents in query results"

                if os.getenv("OPEN_WEBUI_TEST_USER_MAIL") and os.getenv(
                    "OPEN_WEBUI_TEST_USER_API_KEY"
                ):
                    query_results = await knowledge_manager.query_knowledge(
                        knowledge_ids=[knowledge_base_id],
                        query="What fruits are mentioned?",
                        top_k=5,
                        as_user=True,
                    )
                    query_results = query_results.get("documents", [])[0]

                    # Verify we got results
                    assert (
                        len(query_results) == 2
                    ), "Expected query results but got none"

                    for doc in query_results:
                        assert doc in (
                            file_contents[1],
                            file_contents[3],
                        ), "Expected to see only the second and fourth file content"
                else:
                    # If no test user is set, we can't test the user-specific query
                    logging.warning(
                        "Skipping user-specific query test as no test user is set."
                    )
                    pytest.fail(
                        "Test user not set in environment variables. Cannot test user-specific query."
                    )

            finally:
                # Get file IDs for cleanup
                files = await knowledge_manager._retrieve_files_in_knowledge(
                    knowledge_base_id
                )
                # Clean up - remove the added files
                await knowledge_manager.remove_files(
                    knowledge_id=knowledge_base_id, file_ids=[file.id for file in files]
                )

                # Clean up temporary files
                for temp_file in temp_files:
                    if os.path.exists(temp_file):
                        os.unlink(temp_file)
