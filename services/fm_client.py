# FmClient — the cohesive Fusion Manage v3 client.
#
# Wraps core.http_client + a RequestContext. ONE place implements the
# 401-refresh-retry (replacing the old per-module _api() wrapper): every request
# goes through _request(), which on a 401 refreshes the access token via core.auth
# and retries exactly once before giving up.
#
# Endpoint logic lives in cohesive sibling modules (items/workflow/attachments/
# search/fusion_cad); FmClient composes them and exposes one method per operation,
# each 3-5 lines. Methods return plain values + an error string (None on success)
# so callers don't unpack Result objects.

from . import items as _items
from . import workflow as _workflow
from . import attachments as _attachments
from . import search as _search
from . import reports as _reports
from . import bom as _bom
from ..core import auth
from ..core import http_client


class FmClient:
    """Fusion Manage API client bound to a RequestContext."""

    def __init__(self, ctx):
        self.ctx = ctx

    # ------------------------------------------------------------------
    # Core request with single 401-refresh-retry.
    # ------------------------------------------------------------------
    def _request(self, method, url, *, accept='application/json', body=None,
                 content_type=None, extra_headers=None, send_tenant=False,
                 timeout=None, raw=False, empty_body_as=None, with_auth=True):
        """Send a request with the context's identity, retrying once on 401.

        send_tenant -- include the X-Tenant header (workflow /api/rest/v1 endpoints).
        with_auth   -- when False, send no bearer/user headers (pre-signed S3 PUT).
        """
        bearer = self.ctx.bearer if with_auth else None
        user_id = self.ctx.user_id if with_auth else None
        tenant = self.ctx.tenant if (with_auth and send_tenant) else None

        result = http_client.request(
            method, url, bearer=bearer, user_id=user_id, tenant=tenant,
            accept=accept, body=body, content_type=content_type,
            extra_headers=extra_headers, timeout=timeout, raw=raw,
            empty_body_as=empty_body_as)

        if result.unauthorized and with_auth:
            # Token may have expired mid-session. Force a REAL refresh (the cached
            # bearer can still be within its local expiry buffer yet rejected by the
            # server) and retry exactly once.
            new_bearer = self.ctx.refresh_bearer()
            if new_bearer:
                result = http_client.request(
                    method, url, bearer=new_bearer, user_id=user_id, tenant=tenant,
                    accept=accept, body=body, content_type=content_type,
                    extra_headers=extra_headers, timeout=timeout, raw=raw,
                    empty_body_as=empty_body_as)
            # If the 401 survived the retry (or no token could be refreshed), the
            # cached token is unusable — clear it so the auth gate routes to login
            # on the next has_valid_token() check (matches the old add-in behavior).
            if result.unauthorized:
                auth.clear_token_cache()
        return result

    @property
    def base(self):
        return f'https://{self.ctx.tenant}.autodeskplm360.net'

    # ------------------------------------------------------------------
    # Entitlements / workspaces
    # ------------------------------------------------------------------
    def workspaces(self):
        """GET /api/v3/workspaces?unlimited=true — accessible workspace IDs +
        systemName->id map. Returns the same dict shape the old fetch_workspaces did."""
        return _items.workspaces(self)

    # ------------------------------------------------------------------
    # My Work
    # ------------------------------------------------------------------
    def outstanding_work(self):
        """GET /api/v3/users/@me/outstanding-work — processed task list."""
        return _items.outstanding_work(self)

    # ------------------------------------------------------------------
    # Item detail / fields / sections / tableaus
    # ------------------------------------------------------------------
    def item_detail(self, ws, item):
        """GET item. Returns (item_dict, etag, error). error is 'unauthorized' or a message."""
        return _items.item_detail(self, ws, item)

    def view_fields(self, ws, view=1):
        return _items.view_fields(self, ws, view)

    def workspace_fields(self, ws):
        return _items.workspace_fields(self, ws)

    def sections(self, ws):
        return _items.sections(self, ws)

    def tableaus(self, ws):
        return _items.tableaus(self, ws)

    def tableau_data(self, ws, tid, page=1, size=50):
        return _items.tableau_data(self, ws, tid, page, size)

    def items_list(self, ws, offset=0, limit=50):
        """GET /api/v3/workspaces/{ws}/items — paginated item list for any workspace.
        Returns ({'items', 'totalCount', 'offset', 'limit'}, error)."""
        return _items.items_list(self, ws, offset, limit)

    def lookup_options(self, lookup_path, filter_text='', limit=100, offset=0):
        return _items.lookup_options(self, lookup_path, filter_text, limit, offset)

    def image_url(self, relative_link):
        return _items.image_url(self, relative_link)

    # ------------------------------------------------------------------
    # Create / update
    # ------------------------------------------------------------------
    def create_item(self, ws, sections):
        return _items.create_item(self, ws, sections)

    def update_item(self, ws, item, sections, etag=''):
        return _items.update_item(self, ws, item, sections, etag)

    # ------------------------------------------------------------------
    # Workflow transitions
    # ------------------------------------------------------------------
    def transitions(self, ws, item):
        return _workflow.transitions(self, ws, item)

    def run_transition(self, ws, item, transition_id, current_step, comments=''):
        return _workflow.run_transition(self, ws, item, transition_id, current_step, comments)

    # ------------------------------------------------------------------
    # Reporting / dashboard charts (legacy REST v1)
    # ------------------------------------------------------------------
    def report_dashboard(self):
        """GET /api/rest/v1/reports/dashboard — dashboard report list (id/position/link)."""
        return _reports.dashboard(self)

    def report_chart(self, report_id):
        """GET /api/rest/v1/reports/{id}/chart.json — normalized chart (type + series)."""
        return _reports.chart(self, report_id)

    # ------------------------------------------------------------------
    # BOM rows (electronics BOM export)
    # ------------------------------------------------------------------
    def read_bom(self, ws, item_id, view_id=1, depth=100):
        return _bom.read_bom(self, ws, item_id, view_id=view_id, depth=depth)

    def add_bom_row(self, parent_link, child_link, quantity, item_number=None, fields=None):
        return _bom.add_bom_row(self, parent_link, child_link, quantity,
                                item_number=item_number, fields=fields)

    def update_bom_row(self, parent_link, edge_id, child_link, quantity, fields=None,
                       item_number=None):
        return _bom.update_bom_row(self, parent_link, edge_id, child_link, quantity,
                                   fields=fields, item_number=item_number)

    def remove_bom_row(self, edge_link):
        return _bom.remove_bom_row(self, edge_link)

    def item_versions(self, ws, item_id):
        return _bom.item_versions(self, ws, item_id)

    def working_item_id(self, ws, item_id):
        return _bom.working_item_id(self, ws, item_id)

    def bom_row_fields(self, ws):
        """BOM-row (viewdef) field metadata for mapping (e.g. Reference Designators)."""
        return _bom.bom_row_fields(self, ws)

    # ------------------------------------------------------------------
    # Tabs / affected items / permissions
    # ------------------------------------------------------------------
    def item_tabs(self, ws, item):
        return _items.item_tabs(self, ws, item)

    def item_permissions(self, ws, item):
        return _items.item_permissions(self, ws, item)

    def affected_items(self, ws, item, view_id=11, offset=0, limit=100, sort='item.title'):
        return _items.affected_items(self, ws, item, view_id, offset, limit, sort)

    def add_affected(self, ws, item, link_paths):
        return _items.add_affected(self, ws, item, link_paths)

    def remove_affected(self, ws, item, affected_item_id, view_id=11):
        return _items.remove_affected(self, ws, item, affected_item_id, view_id)

    # ------------------------------------------------------------------
    # Search / lineage
    # ------------------------------------------------------------------
    def search_results(self, query_terms, revision=2, limit=50, offset=0, pre_formatted=False):
        return _search.search_results(self, query_terms, revision, limit, offset, pre_formatted)

    def find_item_by_lineage_urn(self, drawings_ws_id, lineage_urn):
        return _search.find_item_by_lineage_urn(self, drawings_ws_id, lineage_urn)

    # ------------------------------------------------------------------
    # Attachments (S3 flow)
    # ------------------------------------------------------------------
    def attachments(self, ws, item):
        return _attachments.list_attachments(self, ws, item)

    def request_upload(self, ws, item, filename, resource_name, file_size,
                       existing_attachment_id=None):
        return _attachments.request_upload(self, ws, item, filename, resource_name,
                                           file_size, existing_attachment_id)

    def upload_to_s3(self, s3_url, extra_headers, file_bytes):
        return _attachments.upload_to_s3(self, s3_url, extra_headers, file_bytes)

    def checkin(self, ws, item, attachment_id):
        return _attachments.checkin(self, ws, item, attachment_id)

    # ------------------------------------------------------------------
    # URL builders (no network)
    # ------------------------------------------------------------------
    def build_item_details_url(self, workspace_id, item_id):
        return _items.build_item_details_url(self.ctx.tenant, workspace_id, item_id)
