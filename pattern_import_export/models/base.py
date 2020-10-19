# Copyright 2020 Akretion France (http://www.akretion.com)
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
import copy
import functools
import logging

from odoo import _, api, models
from odoo.exceptions import ValidationError
from odoo.osv import expression
from odoo.tools import pycompat
from odoo.tools.misc import CountingStream

from odoo.addons.queue_job.job import job

from .common import IDENTIFIER_SUFFIX

_logger = logging.getLogger(__name__)


def is_not_empty(item):
    if not item:
        return False
    elif isinstance(item, dict):
        for key in item:
            if is_not_empty(item[key]):
                return True
    elif isinstance(item, list):
        for subitem in item:
            if is_not_empty(subitem):
                return True
    else:
        return True


class Base(models.AbstractModel):
    _inherit = "base"

    def _helper_build_export_url(self, export):
        base = self.env["ir.config_parameter"].sudo().get_param("web.base.url")
        web = "/web#"
        args = [
            "action="
            + str(self.env.ref("pattern_import_export.action_patterned_imports").id),
            "id=" + str(export.id),
            "model=patterned.import.export",
            "view_type=form",
            "menu_id="
            + str(self.env.ref("pattern_import_export.import_export_menu_root").id),
        ]
        url = "<a href=" + base + web + "&".join(args) + ">" + _("View Job") + "</a>"
        return url

    def _helper_build_export_content_url(self, export):
        base = self.env["ir.config_parameter"].sudo().get_param("web.base.url")
        web = "/web/content/"
        args = [
            "?model=" + "patterned.import.export",
            "id=" + str(export.id),
            "filename_field=datas_fname",
            "field=datas",
            "download=true",
            "filename=" + export.datas_fname,
        ]
        url = "<a href=" + base + web + "&".join(args) + ">" + _("Download") + "</a>"
        return url

    @api.multi
    @job(default_channel="root.exportwithpattern")
    def _generate_export_with_pattern_job(self, export_pattern):
        export = export_pattern._export_with_record(self)
        if export.status == "success":
            self.env.user.notify_success(
                message=_(
                    "Export job has finished. You can access it here: %s"
                    % self._helper_build_export_content_url(export)
                ),
                sticky=True,
            )
        elif export.status == "fail":
            self.env.user.notify_danger(
                message=_(
                    "Export job has failed. You can access it here: %s"
                    % self._helper_build_export_url(export)
                ),
                sticky=True,
            )
        return True

    # There is a native bug in odoo
    # when load records if it fail odoo will rollback and try to load them one by one
    # in order to have explicit error
    # The issue is if the create/write method modify the dict vals
    # the modification will be kept and this can generate issue when loading one by one
    # Do a deepcopy to avoid this issue
    # TODO try to reproduce it on native odoo and open a ticket

    def _load_records_write(self, values):
        return super()._load_records_write(copy.deepcopy(values))

    def _load_records_create(self, values):
        return super()._load_records_create(copy.deepcopy(values))

    def _pattern_format2json(self, row):
        for key in ["id", ".id"]:
            if key in row and row[key] is None:
                row.pop(key)
        res = {}
        items = [(k, v) for k, v in row.items()]
        items.sort()
        for header, vals in items:
            current = res
            previous_key = None
            keys = header.split("|")
            for key in keys:
                if not previous_key:
                    previous_key = key
                elif key.isdigit():
                    if previous_key not in current:
                        current[previous_key] = []
                    key_idx = int(key)
                    if len(current[previous_key]) < int(key_idx):
                        current[previous_key].append({})
                    try:
                        current = current[previous_key][key_idx - 1]
                    except IndexError:
                        raise
                elif not previous_key.isdigit():
                    if previous_key not in current:
                        current[previous_key] = {}
                    current = current[previous_key]
                previous_key = key
            current[keys[-1]] = vals
        return self._post_process_key(res)

    def _clean_identifier_key(self, res, ident_keys):
        for key in ident_keys:
            if key in res:
                res[key.replace(IDENTIFIER_SUFFIX, "")] = res.pop(key)

    def _convert_value_to_domain(self, field_name, value):
        if isinstance(value, dict):
            domain = []
            for key, val in value.items():
                domain.append(("{}.{}".format(field_name, key), "=", val))
        else:
            domain = [(field_name, "=", value)]
        return domain

    def _get_domain_from_identifier_key(self, res):
        ident_keys = []
        domain = []
        for key in list(res.keys()):
            if key.endswith(IDENTIFIER_SUFFIX):
                field_name = key.replace(IDENTIFIER_SUFFIX, "")
                domain = expression.AND(
                    [domain, self._convert_value_to_domain(field_name, res[key])]
                )
                ident_keys.append(key)
        return domain, ident_keys

    def _post_process_o2m_fields(self, res, parent_do_not_exist):
        if ".id" in res:
            parent_id = res[".id"]
        elif "id" in res:
            parent_id = self.env.ref(res["id"]).id
        else:
            parent_id = None

        for key in res:
            field = self._fields.get(key)
            if field and field.type == "one2many":
                subdomain = []
                if parent_id:
                    subdomain.append((field.inverse_name, "=", parent_id))
                # empty subitem are removed
                valid_subitems = []
                for subitem in res[key]:
                    if is_not_empty(subitem):
                        valid_subitems.append(subitem)
                        self.env[field._related_comodel_name]._post_process_key(
                            subitem, subdomain, not bool(parent_id)
                        )
                res[key] = valid_subitems

    def _set_record_id_from_domain(self, res, ident_keys, domain):
        record = self.search(domain)
        if len(record) > 1:
            raise ValidationError(
                _("Too many {} found for the key/value : {}").format(
                    _(record._description), {k: res[k] for k in ident_keys}
                )
            )
        elif record:
            res[".id"] = record.id
            # we remove the key as rewriting the same value is useless
            for key in ident_keys:
                res.pop(key)

    def _post_process_key(self, res, domain=None, parent_do_not_exist=False):
        if domain is None:
            domain = []
        domain_key, ident_keys = self._get_domain_from_identifier_key(res)

        if domain_key and not parent_do_not_exist:
            full_domain = expression.AND([domain, domain_key])
            self._set_record_id_from_domain(res, ident_keys, full_domain)

        self._post_process_o2m_fields(res, parent_do_not_exist=parent_do_not_exist)
        self._clean_identifier_key(res, ident_keys)
        return res

    def _remove_commented_columns(self, row):
        for key in list(row.keys()):
            if key.startswith("#"):
                row.pop(key)

    @api.model
    def _extract_records(self, fields_, data, log=lambda a: None):
        pattern_config = self._context.get("pattern_config")
        if pattern_config:
            partial_commit = pattern_config["partial_commit"]
            flush = self._context["import_flush"]
            for idx, row in enumerate(data):
                self._remove_commented_columns(row)
                if not any(row.values()):
                    continue
                yield self._pattern_format2json(row), {
                    "rows": {"from": idx + 1, "to": idx + 1}
                }
                if idx % pattern_config["flush_step"] == 0:
                    flush()
                    _logger.info("Progress status: record imported {}".format(idx + 1))
                    if partial_commit:
                        # set the model_load savepoint so that in case of error,
                        # rollback to this point
                        self._cr.execute("SAVEPOINT model_load")
            # we force to flush before ending the loop
            # so we can log correctly and commit if needed
            flush()
            _logger.info("Progress status: Total record imported {}".format(idx + 1))
            if partial_commit:
                # so we can update the savepoint
                self._cr.execute("SAVEPOINT model_load")
        else:
            yield from super()._extract_records(fields_, data, log=log)

    # PATCH
    # be careful we redifine the broken native code
    # a pending PR is here:
    # https://github.com/odoo/odoo/pull/60260
    @api.model
    def _convert_records(self, records, log=lambda a: None):
        """ Converts records from the source iterable (recursive dicts of
        strings) into forms which can be written to the database (via
        self.create or (ir.model.data)._update)

        :returns: a list of triplets of (id, xid, record)
        :rtype: list((int|None, str|None, dict))
        """
        field_names = {name: field.string for name, field in self._fields.items()}
        if self.env.lang:
            field_names.update(self.env["ir.translation"].get_field_string(self._name))

        convert = self.env["ir.fields.converter"].for_model(self)

        def _log(base, record, field, exception):
            type = "warning" if isinstance(exception, Warning) else "error"
            # logs the logical (not human-readable) field name for automated
            # processing of response, but injects human readable in message
            exc_vals = dict(base, record=record, field=field_names[field])
            record = dict(
                base,
                type=type,
                record=record,
                field=field,
                message=pycompat.text_type(exception.args[0]) % exc_vals,
            )
            if len(exception.args) > 1 and exception.args[1]:
                record.update(exception.args[1])
            log(record)

        stream = CountingStream(records)
        for record, extras in stream:
            # xid
            xid = record.get("id", False)
            # dbid
            dbid = False
            if ".id" in record:
                try:
                    dbid = int(record[".id"])
                except ValueError:
                    # Code changed
                    if self._fields["id"].type != "integer":
                        # in case of overridden id column
                        dbid = record[".id"]
                    else:
                        log(
                            dict(
                                extras,
                                type="error",
                                record=stream.index,
                                field=".id",
                                message=_(u"Invalid database identifier '%s'") % dbid,
                            )
                        )
                    # End of code changed
                if not self.search([("id", "=", dbid)]):
                    log(
                        dict(
                            extras,
                            type="error",
                            record=stream.index,
                            field=".id",
                            message=_(u"Unknown database identifier '%s'") % dbid,
                        )
                    )
                    dbid = False

            converted = convert(record, functools.partial(_log, extras, stream.index))

            yield dbid, xid, converted, dict(extras, record=stream.index)
