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
