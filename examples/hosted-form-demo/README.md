# SharpToolz hosted-form demo

This folder behaves like a separate customer's website. PaperPilot owns the surrounding HTML and CSS, while SharpToolz supplies the template API and secure hosted form.

The demo backend keeps the API key server-side. The browser receives only template metadata and a short-lived embed URL.

## Run locally

Make sure Redis is running, then use four terminals from the SharpToolz project directory.

1. Import or refresh the real travel-document samples:

   ```bash
   cd backend
   .venv/bin/python manage.py seed_api_demo
   ```

2. Start the SharpToolz backend with the matching frontend URL:

   ```bash
   cd backend
   FRONTEND_URL=http://127.0.0.1:5173 .venv/bin/python manage.py runserver 127.0.0.1:8137
   ```

3. Start the SharpToolz render worker in another terminal:

   ```bash
   cd backend
   .venv/bin/celery -A serverConfig worker --loglevel=info --pool=solo --concurrency=1
   ```

4. Start the SharpToolz frontend:

   ```bash
   cd frontend
   pnpm dev --host 127.0.0.1 --port 5173
   ```

5. Issue an isolated development key and start PaperPilot in the same shell:

   ```bash
   export SHARPTOOLZ_API_KEY="$(cd backend && .venv/bin/python manage.py issue_api_demo_key --username admin --origin http://127.0.0.1:4188)"
   python3 backend/examples/hosted-form-demo/server.py
   ```

Open <http://127.0.0.1:4188/#templates>. Choose any sample, edit its real SharpToolz form, and select **Create document**. PaperPilot will show the finished PNG after the render completes.

The samples are real templates from the repository: two boarding passes and two flight itineraries. **Boarding Pass 1** uses the protected Canvas proof: the iframe receives flattened raster artwork and editable-layer metadata, never the source SVG. The other cards retain the standard embed for comparison. Test-mode documents include the expected watermark.

Re-running `issue_api_demo_key` revokes the previous key named `Hosted form demo`. The raw key is kept only in the shell environment and is never written into the HTML or repository.
