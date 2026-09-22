# SharpToolz Python SDK

```bash
# Available after the first PyPI release
pip install sharptoolz
```

```python
import os
from sharptoolz import SharpToolz

with SharpToolz(api_key=os.environ["SHARPTOOLZ_API_KEY"]) as sharp:
    session = sharp.hosted_forms.create(
        template_id=template_id,
        external_user_id=current_user_id,
        origin="https://app.example.com",
        mode="test",
        preview_mode="protected",
    )

    # Return session["embed_url"] to your frontend.
```

Only browser JavaScript mounts the returned URL with `@sharp-toolz/sdk/browser`.
Python keeps the API key on your backend and can create or edit hosted sessions,
list documents, and wait for PNG/PDF renders over a job-scoped WebSocket.

Render and save a PDF on your server:

```python
job = sharp.documents.render_and_wait(document_id, format="pdf")
sharp.renders.download_to(job, "document.pdf")
```

`render_and_wait()` returns render metadata. `renders.download()` returns file
bytes and `renders.download_to()` writes those bytes to a path. Both methods
retrieve the render job first so they use a fresh five-minute signed URL, and
they never send the API key to that URL.

Render artifacts are retained for 24 hours by default. Retrieving a completed
render job creates a new download URL while the artifact remains available.
