from asgiref.sync import sync_to_async
from ....models import Template, PurchasedTemplate
from wallet.models import Wallet

async def handle_load_template(args):
    events = []
    template_id = args.get("template_id", "")
    try:
        def _load(tid):
            tpl = Template.objects.filter(pk=tid, is_active=True).select_related("tool").first()
            if not tpl: return None
            return {
                "id": str(tpl.id),
                "name": tpl.name,
                "toolName": tpl.tool.name if tpl.tool else "General",
                "price": str(tpl.tool.price) if tpl.tool else "0",
                "banner": tpl.banner.url if tpl.banner else "",
                "field_count": len(tpl.form_fields or []),
            }
        tpl_data = await sync_to_async(_load)(template_id)
        if tpl_data:
            events.append({"type": "template_loaded", "template": tpl_data})
            return {"events": events, "text": f"Loaded '{tpl_data['name']}' ({tpl_data['field_count']} fields)."}
        return {"events": events, "text": f"Template {template_id} not found."}
    except Exception as exc:
        return {"events": events, "text": f"Load failed: {exc}"}

async def handle_purchase_template(args, user):
    events = []
    template_id = args.get("template_id", "")
    form_fields = args.get("form_fields", [])
    try:
        def _purchase(tid, flds, u):
            tpl = Template.objects.select_related("tool").get(pk=tid)
            if not tpl.tool: return None, "No tool linked"
            wallet, _ = Wallet.objects.get_or_create(user=u)
            if wallet.spendable_balance < tpl.tool.price: return None, "Insufficient balance"
            wallet.debit(tpl.tool.price, description=f"Tool purchase: {tpl.tool.name}")
            pt = PurchasedTemplate.objects.create(
                buyer=u, template=tpl, form_fields=flds, price_paid=tpl.tool.price
            )
            return {
                "id": str(pt.id),
                "name": tpl.name,
                "price": str(tpl.tool.price),
                "new_balance": str(wallet.balance),
            }, None
        pt_data, error = await sync_to_async(_purchase)(template_id, form_fields, user)
        if error: return {"events": events, "text": f"Purchase failed: {error}"}
        events.append({"type": "purchased", "template": pt_data})
        return {"events": events, "text": f"Successfully purchased {pt_data['name']}!"}
    except Exception as exc:
        return {"events": events, "text": f"Purchase error: {exc}"}

async def handle_download_document(args, user):
    events = []
    pt_id = args.get("purchased_template_id", "")
    out_type = args.get("output_type", "pdf")
    try:
        def _get_pt(pid, u):
            return PurchasedTemplate.objects.filter(pk=pid, buyer=u).first()
        pt = await sync_to_async(_get_pt)(pt_id, user)
        if not pt: return {"events": events, "text": "Purchased document not found."}
        from ...actions import DownloadDoc
        from django.test import RequestFactory
        import base64
        import json
        
        factory = RequestFactory()
        req = factory.post('/api/download-doc/', 
                          data=json.dumps({"purchased_template_id": str(pt_id), "type": out_type}),
                          content_type='application/json')
        req.user = user
        
        # We need async to sync wrapper for the view
        def _get_doc():
            response = DownloadDoc.as_view()(req)
            if response.status_code == 200:
                b64 = base64.b64encode(response.content).decode('utf-8')
                mime = "application/pdf" if out_type == "pdf" else "image/png"
                return f"data:{mime};base64,{b64}", None
            return None, response.data.get("error", "Unknown error")
            
        file_data, error = await sync_to_async(_get_doc)()
        if error:
            return {"events": events, "text": f"Document compilation failed: {error}"}
            
        events.append({
            "type": "document_ready", 
            "file": {
                "data": file_data, 
                "filename": f"{pt.name or 'document'}.{out_type}",
                "mime": "application/pdf" if out_type == "pdf" else "image/png"
            }
        })
        return {"events": events, "text": f"Your {out_type.upper()} is ready for download!"}
    except Exception as exc:
        return {"events": events, "text": f"Download failed: {exc}"}

async def handle_save_edits(args, user):
    events = []
    pt_id = args.get("purchased_template_id", "")
    form_fields = args.get("form_fields", [])
    try:
        def _save(pid, flds, u):
            pt = PurchasedTemplate.objects.filter(pk=pid, buyer=u).first()
            if not pt: return False
            pt.form_fields = flds
            pt.save()
            return True
        success = await sync_to_async(_save)(pt_id, form_fields, user)
        if success: return {"events": events, "text": "Edits saved successfully."}
        return {"events": events, "text": "Document not found."}
    except Exception as exc:
        return {"events": events, "text": f"Save failed: {exc}"}

async def handle_get_template_details(args):
    events = []
    template_id = args.get("template_id", "")
    try:
        def _get_details(tid):
            tpl = Template.objects.filter(pk=tid, is_active=True).first()
            if not tpl: return None
            fields = []
            for f in (tpl.form_fields or []):
                fields.append({
                    "name": f.get("name", f.get("id", "")),
                    "type": f.get("type", "text"),
                    "required": f.get("required", False)
                })
            return {
                "id": str(tpl.id),
                "name": tpl.name,
                "fields": fields
            }
        details = await sync_to_async(_get_details)(template_id)
        if details:
            field_list = "\n".join([f"- {f['name']} ({f['type']})" for f in details['fields']])
            return {
                "events": events, 
                "text": f"Required fields for '{details['name']}':\n{field_list}"
            }
        return {"events": events, "text": f"Template {template_id} not found."}
    except Exception as exc:
        return {"events": events, "text": f"Failed to get details: {exc}"}
