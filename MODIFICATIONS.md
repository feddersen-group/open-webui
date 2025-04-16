# Modifications to Upstream

This repository adapts the upstream openwebui repository in the following ways:

## Knowledge-Base per File Authentication for User and his Entra-Groups

We allow to pass metadata through the file upload route with the type ExtraMeta. This is a dictionary
of the following style:

```python
class ItemPermissions(BaseModel):
    users: list[str] = Field(
        alias="user_ids",
        description="List of users who have access to the file",
        default=[],
    )
    groups: list[str] = Field(
        alias="group_ids",
        description="List of groups who have access to the file",
        default=[],
    )

    model_config = {"populate_by_name": True}


class ItemMetadata(BaseModel):
    title: str = Field(alias="itemTitle", description="Title of the item")
    url: str = Field(alias="itemUrl", description="URL of the item, direct link to it")
    context_url: Optional[str] = Field(
        alias="contextItemUrl",
        description="URL of the item's context, e.g. the page it is on",
        default=None,
    )
    date: str = Field(
        alias="itemDate",
        description="Date of the item, e.g. the date of a news article",
    )
    source: Optional[str] = Field(
        alias="itemSource",
        description="Source of the item (which workflow produced it)",
    )

    model_config = {"populate_by_name": True}


class ExtraMetadata(BaseModel):
    auth: ItemPermissions
    metadata: ItemMetadata
```

This functionality is implemented for the pgvector backend but can easily be adapted for other
vector stores. See `backend/feddersen` for the respective classes.

When a user makes a chat request to our model with private knowledge bases, the vector store has
only access to files where the user actually has the permissions to see it. This is only concerning
knowledge bases that were created and filled through the API. 
For authentication, the user's email is checked against a list of user emails and also his Entra 
groups, which are retrieved via Graph API, are checked if any of them are allowed for the user to
see it.

To allow this functionality, I needed to pass the user from the retrieval routes everywhere through
to the vector retrieving step. 

### Activating this

You need to set `VECTOR_DB=pgvector_feddersen`. It will create a new DB table called `DocumentAuthChunk` with an extra column for auth and will store the `ItemMetadata` within the
 `vmetadata` column, nested as `EXTRA_METADATA_MIDDLEWARE_KEY`, which is set in `backend/feddersen/config`. 

The app registration used for EntraID needs to have the right (application-permission) to retrieve
groups for this to work. 

## Frontend changes

- Deactivated that the model can be changed in a chat after it has begun (in order to stop leakage 
of information when starting with documents in EU and changing to a model hosted in US).
- Don't show "content" in citation modal - not of interest for many
- Citation Modal shows `itemUrl` and `contextItemUrl` to refer to the external source where the 
file can be viewed (e.g. intranet).

## Further changes

- OAuth group assignment does **not** use the group-claim of the token but the graph api retrieving to
allow >100 groups. For internal reasons we only use and retrieve groups that start with "-". 
- We use Gemini for PDF-Extraction which is called through the default OpenAI Endpoint set (with 
these credentials, too) because we use a LiteLLM-Proxy. 