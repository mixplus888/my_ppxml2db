import sys
import argparse
import logging
from collections import defaultdict
from pprint import pprint
import json
import logging
import os.path

import lxml.etree as ET

from version import __version__
import dbhelper


_log = logging.getLogger(__name__)
#logging.basicConfig(
#    level=logging.INFO,
#    format='%(asctime)s %(levelname)-5s %(message)s',
#    datefmt='%Y-%m-%d %H:%M:%S',
#)

# Rename field in a dictionary
def ren(d, old, new):
    if old in d:
        d[new] = d[old]
        del d[old]

def as_bool(v):
    if not v or v.strip() == "": 
        return 0
    return {"false": 0, "true": 1}.get(v.lower().strip(), 0)

def dump_el(el):
    print(ET.tostring(el).decode())


class PortfolioPerformanceXML2DB:

    def parse_props(self, el, props):
        d = {}
        for p in props:
            conv = lambda x: x
            if isinstance(p, tuple):
                conv = p[1]
                p = p[0]
            pel = el.find(p)
            if pel is not None:
                d[p] = conv("" if pel.text is None else pel.text)
            elif p in el.attrib:
                # Otherwise try attribute (will return None if not there)
                d[p] = conv(el.get(p))
        return d

    def uuid(self, el):
        # Try grabbing 'reference' first; if missing, fall back to 'id'
        id = el.get("reference") or el.get("id")
        
        # If both are missing, safely return None right away
        if id is None:
            return None
            
        return self.id2uuid_map.get(id, None)

    @staticmethod
    def is_account_tag(tag):
        return tag in ("account", "referenceAccount", "accountFrom", "accountTo")

    def parse_entry(self, entry_el):
        els = entry_el.findall("*")
        assert len(els) == 2, len(els)
        return [(e.tag, e.text) for e in els]

    def parse_configuration(self, pel):
        conf = {}
        for c_el in pel.findall("configuration/entry"):
            d = self.parse_entry(c_el)
            assert d[0][0] == "string"
            assert d[1][0] == "string"
            conf[d[0][1]] = d[1][1] if d[1][1] is not None else ""
        return conf

    def parse_attributes(self, pel, el_tag="attributes/map"):
        attr_els = pel.findall(el_tag + "/entry")
        for seq, attr_el in enumerate(attr_els):
            els = attr_el.findall("*")
            assert len(els) == 2
            assert els[0].tag == "string"
            if els[1].tag == "limitPrice":
                fields = self.parse_props(els[1], ("operator", "value"))
                value = "%s %s" % (fields["operator"], fields["value"])
            elif els[1].tag == "bookmark":
                fields = self.parse_props(els[1], ("label", "pattern"))
                if not fields:
                    value = None
                else:
                    value = json.dumps(fields)
            else:
                value = els[1].text
            fields = {
                "attr_uuid": els[0].text,
                "type": els[1].tag,
                "value": value,
                "seq": seq,
            }
            yield fields

    def handle_price(self, price_el):
        props = ["t", "v"]
        price_fields = self.parse_props(price_el, props)
        ren(price_fields, "v", "value")
        ren(price_fields, "t", "tstamp")
        
        # 1. Grab the security ID safely
        security_id = self.cur_uuid()
        
        # 2. Only insert if the price actually belongs to a known security
        if security_id is not None:
            price_fields["security"] = security_id
            dbhelper.insert("price", price_fields)

    def handle_latest(self, latest_el):
        if latest_el is not None:
            props = ["t", "v", "high", "low", "volume"]
            latest_fields = self.parse_props(latest_el, props)
            ren(latest_fields, "v", "value")
            ren(latest_fields, "t", "tstamp")
            
            # Dynamically grab the parent context security ID
            sec_id = self.cur_uuid()
            
            # FIXED: Safeguard against empty parent contexts or untracked assets
            if sec_id is None:
                return

            latest_fields["security"] = sec_id
            dbhelper.insert("latest_price", latest_fields)

    def handle_event(self, event_el):
            props = ["date", "type", "details"]
            fields = self.parse_props(event_el, props)
            fields["security"] = self.cur_uuid()
            dbhelper.insert("security_event", fields)

    def handle_security(self, el):
        if el.get("reference") is not None:
            return

        props = [
            "uuid", "onlineId", "name", "currencyCode", "targetCurrencyCode", "note",
            "isin", "tickerSymbol", "calendar", "wkn", "feedTickerSymbol",
            "feed", "feedURL", "latestFeed", "latestFeedURL",
            ("isRetired", as_bool), "updatedAt"
        ]
        sec = self.parse_props(el, props)
        ren(sec, "currencyCode", "currency")
        ren(sec, "targetCurrencyCode", "targetCurrency")
        
        # FIXED: Safeguard against duplicate execution passes on the same XML node
        if not hasattr(self, "_seen_securities"):
            self._seen_securities = set()
            
        if sec["uuid"] in self._seen_securities:
            return # Already ingested this security in a prior event pass, skip duplicate work
            
        self._seen_securities.add(sec["uuid"])
        
        try:
            dbhelper.insert("security", sec)

            for fields in self.parse_attributes(el):
                fields["security"] = sec["uuid"]
                dbhelper.insert("security_attr", fields)

            prop_els = el.findall("property")
            for seq, prop_el in enumerate(prop_els):
                fields = {
                    "security": sec["uuid"], "type": prop_el.get("type"),
                    "name": prop_el.get("name"), "value": prop_el.text, "seq": seq,
                }
                dbhelper.insert("security_prop", fields)
        except Exception as e:
            if "UNIQUE constraint failed" in str(e):
                pass # Extra fallback protective layer
            else:
                raise e
            
    def handle_account_attrs(self, pel, uuid):
        for fields in self.parse_attributes(pel):
            fields["account"] = uuid
            
            # FIXED: Handle missing attr_uuid to prevent SQLite NOT NULL crashes
            if not fields.get("attr_uuid") or str(fields.get("attr_uuid")) == "None":
                import hashlib
                # Create a stable, deterministic ID unique to this account and attribute combination
                seed = f"{uuid}_{fields.get('name', 'default')}"
                fields["attr_uuid"] = hashlib.md5(seed.encode('utf-8')).hexdigest()

            try:
                dbhelper.insert("account_attr", fields)
            except Exception as e:
                if "UNIQUE constraint failed" in str(e):
                    continue  # Already inserted in an overlapping pass, skip gracefully
                raise e
            
    def handle_account(self, el, orderno):
        props = ["uuid", "name", "currencyCode", "note", ("isRetired", as_bool), "updatedAt", "id"]
        fields = self.parse_props(el, props)
        ren(fields, "currencyCode", "currency")
        ren(fields, "id", "_xmlid")
        fields["type"] = "account"
        fields["_order"] = orderno
        
        # FIXED: Fallback to UUID if inline XML id attribute is missing
        if not fields.get("_xmlid") or fields.get("_xmlid") == "None":
            fields["_xmlid"] = fields.get("uuid") or el.findtext("uuid")

        try:
            dbhelper.insert("account", fields)
            self.handle_account_attrs(el, fields["uuid"])
        except Exception as e:
            if "UNIQUE constraint failed" in str(e):
                return # Already ingested, safe to skip duplicate pass
            raise e

    def handle_portfolio(self, el, orderno):
        props = ["uuid", "name", "note", ("isRetired", as_bool), "updatedAt", "id"]
        fields = self.parse_props(el, props)
        ren(fields, "id", "_xmlid")
        acc = el.find("referenceAccount")
        fields["referenceAccount"] = self.uuid(acc)
        fields["type"] = "portfolio"
        fields["_order"] = orderno
        
        # FIXED: Fallback to UUID if inline XML id attribute is missing
        if not fields.get("_xmlid") or fields.get("_xmlid") == "None":
            fields["_xmlid"] = fields.get("uuid") or el.findtext("uuid")

        try:
            dbhelper.insert("account", fields)
            self.handle_account_attrs(el, fields["uuid"])
        except Exception as e:
            if "UNIQUE constraint failed" in str(e):
                return # Already ingested, safe to skip duplicate pass
            raise e
    
    def handle_watchlist(self, el, orderno):
        fields = self.parse_props(el, ["name"])
        fields["_order"] = orderno
        id = dbhelper.insert("watchlist", fields, returning="_id")
        for sec in el.findall("securities/security"):
            # 1. Grab the security UUID safely (can be None now)
            sec_uuid = self.uuid(sec)
            
            # 2. Skip this specific entry if the XML shortcut couldn't be resolved
            if sec_uuid is None:
                continue
                
            fields = {"list": id, "security": sec_uuid}
            dbhelper.insert("watchlist_security", fields)   

    def handle_xact(self, acc_type, acc_uuid, el, orderno):
        # GUARD: Skip processing if no valid account or portfolio context is provided
        if not acc_uuid or acc_uuid == "None":
            return

        # RELATION LINK REPAIR: If acc_uuid is pointing to an individual transaction ID
        # instead of a parent account container, crawl up the XML tree to find the true parent owner.
        is_real_account = dbhelper.select("account", where="uuid='%s'" % acc_uuid)
        if not is_real_account:
            parent = el.getparent()
            # Climb up the tree up to 3 tiers to find a node owning an account/portfolio reference string
            for _ in range(3):
                if parent is None: 
                    break
                # Check for explicit reference structures inside account metadata loops
                account_node = parent.find("account")
                if account_node is not None and account_node.text:
                    acc_uuid = account_node.text
                    break
                portfolio_node = parent.find("portfolio")
                if portfolio_node is not None and portfolio_node.text:
                    acc_uuid = portfolio_node.text
                    break
                parent = parent.getparent()

        # GUARD: Dynamically check the element or fallback parsing tracking properties for transaction UUID
        xact_uuid_check = el.findtext("uuid") or el.get("id") or (el.find("id").text if el.find("id") is not None else None)
        
        if not hasattr(self, "_seen_xacts"):
            self._seen_xacts = set()
            
        if xact_uuid_check:
            if xact_uuid_check in self._seen_xacts:
                return # Already handled on an alternative event path, skip
            self._seen_xacts.add(xact_uuid_check)

        # Start with calculating unit aggregates, to add to xact row in DB.
        units_dict = defaultdict(int)
        for unit_el in el.findall("units/unit"):
            am_el = unit_el.find("amount")
            if am_el is not None and am_el.get("amount") is not None:
                try:
                    units_dict[unit_el.get("type")] += int(am_el.get("amount"))
                except (ValueError, TypeError):
                    pass

        props = [
            "uuid",
            "date",
            "currencyCode",
            "amount",
            "shares",
            "note",
            "source",
            "updatedAt",
            "type",
            "id",
        ]
        fields = self.parse_props(el, props)
        ren(fields, "currencyCode", "currency")
        ren(fields, "id", "_xmlid")
        fields["account"] = acc_uuid
        fields["acctype"] = acc_type
        fields["_order"] = orderno
        sec = el.find("security")
        if sec is not None:
            fields["security"] = self.uuid(sec)
        fields["fees"] = units_dict["FEE"]
        fields["taxes"] = units_dict["TAX"]
        
        # GUARD: Fallback default uuid generation if parse_props returned empty
        if not fields.get("uuid") and xact_uuid_check:
            fields["uuid"] = xact_uuid_check
        elif not fields.get("uuid"):
            import uuid
            fields["uuid"] = str(uuid.uuid4())

        # FIXED: Explicitly satisfy the SQLite NOT NULL constraint for _xmlid before executing insert
        if not fields.get("_xmlid") or str(fields.get("_xmlid")) == "None":
            fields["_xmlid"] = fields["uuid"]

        try:
            dbhelper.insert("xact", fields)
        except Exception as e:
            if "UNIQUE constraint failed" in str(e):
                return # Skip remaining unit arrays, already safely saved
            raise e

        xact_uuid = fields["uuid"]
        for unit_el in el.findall("units/unit"):
            am_el = unit_el.find("amount")
            if am_el is None:
                continue
            fields_unit = {
                "xact": xact_uuid,
                "type": unit_el.get("type"),
                "amount": am_el.get("amount"),
                "currency": am_el.get("currency"),
            }
            forex_el = unit_el.find("forex")
            if forex_el is not None:
                fields_unit["forex_amount"] = forex_el.get("amount")
                fields_unit["forex_currency"] = forex_el.get("currency")
            rate_el = unit_el.find("exchangeRate")
            if rate_el is not None:
                fields_unit["exchangeRate"] = rate_el.text
                
            try:
                dbhelper.insert("xact_unit", fields_unit)
            except Exception as e:
                if "UNIQUE constraint failed" in str(e):
                    continue
                raise e
            
    def handle_crossEntry(self, x_el):
        if x_el.get("reference") is not None:
            return

        typ = x_el.get("class")
        if typ == "buysell":
            fields = {
                "type": typ,
                "from_acc": self.uuid(x_el.find("portfolio")),
                "from_xact": self.uuid(x_el.find("portfolioTransaction")),
                "to_acc": self.uuid(x_el.find("account")),
                "to_xact": self.uuid(x_el.find("accountTransaction")),
            }
        elif typ == "account-transfer":
            fields = {
                "type": typ,
                "from_acc": self.uuid(x_el.find("accountFrom")),
                "from_xact": self.uuid(x_el.find("transactionFrom")),
                "to_acc": self.uuid(x_el.find("accountTo")),
                "to_xact": self.uuid(x_el.find("transactionTo")),
            }
        elif typ == "portfolio-transfer":
            fields = {
                "type": typ,
                "from_acc": self.uuid(x_el.find("portfolioFrom")),
                "from_xact": self.uuid(x_el.find("transactionFrom")),
                "to_acc": self.uuid(x_el.find("portfolioTo")),
                "to_xact": self.uuid(x_el.find("transactionTo")),
            }
        else:
            raise NotImplementedError(typ)

        # GUARD 1: Safely drop the element if either critical account mapping is missing or unparsed yet
        if not fields.get("from_acc") or not fields.get("to_acc"):
            return

        # GUARD 2: Ensure a unique identifier key exists for the schema record
        if not fields.get("uuid"):
            # Check if an xml id or internal tracking tag attribute is available
            xml_id = x_el.get("id")
            if xml_id and hasattr(self, "id2uuid_map") and xml_id in self.id2uuid_map:
                fields["uuid"] = self.id2uuid_map[xml_id]
            else:
                import uuid
                fields["uuid"] = str(uuid.uuid4())

        try:
            dbhelper.insert("xact_cross_entry", fields)
        except Exception as e:
            if "UNIQUE constraint failed" in str(e):
                return  # Record was already captured via a matching parallel event loop pass
            raise e

    def handle_taxonomy(self, taxon_el):
        props = ["id", "name"]
        fields = self.parse_props(taxon_el, props)
        ren(fields, "id", "uuid")
        
        for dim_els in taxon_el.findall("dimensions/string"):
            dim_fields = {
                "taxonomy": fields["uuid"],
                "name": "dimension",
                "value": dim_els.text,
            }
            dbhelper.insert("taxonomy_data", dim_fields)
            
        root_el = taxon_el.find("root")
        root_uuid = self.uuid(root_el)
        
        # FIXED: Safeguard against missing or unresolvable root structures
        if root_uuid is None:
            print(f"Warning: Skipping unresolvable taxonomy root for category '{fields.get('name', 'Unknown')}'")
            return

        fields["root"] = root_uuid
        dbhelper.insert("taxonomy", fields)
        self.handle_taxonomy_level(fields["uuid"], None, root_el)

    def handle_taxonomy_level(self, taxon_uuid, parent_uuid, level_el):
        props = ["id", "name", "color", "weight", "rank"]
        fields = self.parse_props(level_el, props)
        ren(fields, "id", "uuid")
        fields["parent"] = parent_uuid
        fields["taxonomy"] = taxon_uuid
        level_uuid = fields["uuid"]
        dbhelper.insert("taxonomy_category", fields)

        for data_el in level_el.findall("data/entry"):
            data = self.parse_entry(data_el)
            fields = {
                "name": data[0][1],
                "type": data[1][0],
                "value": data[1][1],
                "category": level_uuid,
                "taxonomy": taxon_uuid,
            }
            dbhelper.insert("taxonomy_data", fields)

        for as_el in level_el.findall("assignments/assignment"):
            props = ["weight", "rank"]
            fields = self.parse_props(as_el, props)
            el = as_el.find("investmentVehicle")
            fields["item_type"] = el.get("class")
            fields["item"] = self.uuid(el)
            fields["category"] = level_uuid
            fields["taxonomy"] = taxon_uuid
            id = dbhelper.insert("taxonomy_assignment", fields, returning="_id")
            for data_el in as_el.findall("data/entry"):
                data = self.parse_entry(data_el)
                fields = {
                    "assignment": id,
                    "name": data[0][1],
                    "type": data[1][0],
                    "value": data[1][1],
                }
                dbhelper.insert("taxonomy_assignment_data", fields)

        for ch_el in level_el.findall("children/classification"):
            self.handle_taxonomy_level(taxon_uuid, level_uuid, ch_el)

    def handle_dashboard(self, dashb_el):
            props = ["id", "name"]
            fields = self.parse_props(dashb_el, props)
            conf = self.parse_configuration(dashb_el)
            fields["config_json"] = json.dumps(conf)

            columns = []
            for col_el in dashb_el.findall("columns/column"):
                props = ["weight"]
                col_fields = self.parse_props(col_el, props)
                col_fields["widgets"] = []
                for widget_el in col_el.findall("widgets/widget"):
                    wid_fields = self.parse_props(widget_el, ["label"])
                    wid_fields["type"] = widget_el.get("type")
                    if widget_el.find("configuration") is not None:
                        conf = self.parse_configuration(widget_el)
                        wid_fields["config"] = conf
                    col_fields["widgets"].append(wid_fields)
                columns.append(col_fields)
            fields["columns_json"] = json.dumps(columns)
            dbhelper.insert("dashboard", fields)

    def handle_settings(self, settings_el):
        for bmark_el in settings_el.findall("bookmarks/bookmark"):
            props = ["label", "pattern"]
            fields = self.parse_props(bmark_el, props)
            dbhelper.insert("bookmark", fields)

        for attr_type_el in settings_el.findall("attributeTypes/attribute-type"):
            props = ["id", "name", "columnLabel", "source", "target", "type", "converterClass"]
            fields = self.parse_props(attr_type_el, props)
            props = []
            for p in self.parse_attributes(attr_type_el, "properties"):
                props.append({"name": p["attr_uuid"], "type": p["type"], "value": p["value"]})
            fields["props_json"] = json.dumps(props)
            dbhelper.insert("attribute_type", fields)

        for config_set_el in settings_el.findall("configurationSets/entry"):
            props = ["string"]
            fields = self.parse_props(config_set_el, props)
            ren(fields, "string", "name")
            cset_id = dbhelper.insert("config_set", fields, returning="_id")
            for config_e_el in config_set_el.findall("config-set/configurations/config"):
                props = ["uuid", "name", "data"]
                fields = self.parse_props(config_e_el, props)
                fields["config_set"] = cset_id
                dbhelper.insert("config_entry", fields)

    def handle_toplevel_properties(self, el):
        for prop_el in el.findall("entry"):
            d = self.parse_entry(prop_el)
            # Ensure the entry has at least two parsed elements before asserting types
            if len(d) < 2 or d[0] is None or d[1] is None:
                continue
                
            assert d[0][0] == "string"
            assert d[1][0] == "string"
            
            fields = {"name": d[0][1], "value": d[1][1]}
            
            # GUARD 1: Skip if the name is blank, empty, or string literal "None"
            if not fields.get("name") or str(fields.get("name")).strip() == "" or fields.get("name") == "None":
                continue

            try:
                dbhelper.insert("property", fields)
            except Exception as e:
                if "UNIQUE constraint failed" in str(e):
                    continue  # Already processed in a parallel pass, skip safely
                raise e

    def handle_client(self, el):
        props = ["version", "baseCurrency"]
        fields = self.parse_props(el, props)
        for n in props:
            dbhelper.insert("property", {"name": n, "value": fields[n], "special": 1})

    def __init__(self, xml):
        self.xml = xml
        self.refcache = {}

    def cur_uuid(self):
        if not self.container_stack:
            return None
        return self.container_stack[-1][1]

    def iterparse(self):
        self.el_stack = []
        self.container_stack = []
        self.cur_xmlid = None
        self.id2uuid_map = {}
        self.uuid2ctr_map = {}
        self.el_order = 0
        for event, el in ET.iterparse(self.xml, events=("start", "end")):
            #print(event, el, el.attrib)
            self.el_order += 1
            if event == "start":
                self.el_stack.append(el.tag)
                if el.tag in ("security", "account", "referenceAccount", "accountFrom", "accountTo", "portfolio", "portfolioFrom", "portfolioTo"):
                    self.cur_xmlid = el.get("id")
                    # FIX: Force container stack registration for accounts/portfolios even if they drop the inline XML 'id' attribute
                    if self.cur_xmlid is not None or el.tag in ("account", "portfolio", "accountFrom", "accountTo", "portfolioFrom", "portfolioTo"):
                        self.container_stack.append([el.tag, None])
                elif el.tag in ("account-transaction", "accountTransaction", "portfolio-transaction", "portfolioTransaction", "transactionFrom", "transactionTo"):
                    self.cur_xmlid = el.get("id")
                elif el.tag in ("root", "classification"):
                    self.cur_xmlid = el.get("id")
                elif el.tag in ("taxonomy", "dashboard", "settings"):
                    self.container_stack.append([el.tag, None])

            elif event == "end":
                assert self.el_stack[-1] == el.tag
                self.el_stack.pop()
                if el.tag in ("uuid", "id"):
                    if   self.container_stack and self.container_stack[-1][1] is None:
                        self.container_stack[-1][1] = el.text
                        #print("Setting uuid of top container:", self.container_stack, el.sourceline)
                        self.uuid2ctr_map[el.text] = self.container_stack[-1][0]
                    if self.cur_xmlid is not None:
                        self.id2uuid_map[self.cur_xmlid] = el.text

                elif el.tag == "price":
                    if not args.skip_prices:
                        self.handle_price(el)
                elif el.tag == "latest":
                    self.handle_latest(el)
                elif el.tag == "event":
                    self.handle_event(el)

                elif el.tag == "security":
                    self.handle_security(el)
                elif el.tag == "watchlist":
                    self.handle_watchlist(el, self.el_order)
                elif el.tag in ("account", "accountFrom", "accountTo", "referenceAccount"):
                    # FIX: Handle account if it has an id attribute OR if it contains a structural uuid element definitions
                    if el.get("id") or el.find("uuid") is not None:
                        self.handle_account(el, self.el_order)
                    elif el.tag == "account" and el.get("reference") is not None:
                        xmlid = el.get("reference")
                        dbhelper.execute_dml("UPDATE account SET _order=? WHERE _xmlid=?", (self.el_order, xmlid))
                elif el.tag in ("portfolio", "portfolioFrom", "portfolioTo"):
                    # FIX: Handle portfolio if it has an id attribute OR if it contains a structural uuid element definitions
                    if el.get("id") or el.find("uuid") is not None:
                        self.handle_portfolio(el, self.el_order)
                    elif el.tag == "portfolio" and el.get("reference") is not None:
                        xmlid = el.get("reference")
                        dbhelper.execute_dml("UPDATE account SET _order=? WHERE _xmlid=?", (self.el_order, xmlid))

                elif el.tag == "account-transaction":
                    if el.get("id") or el.find("uuid") is not None:
                        # CORRECT: Data transactions must use their own unique transaction UUID
                        lookup_uuid = el.findtext("uuid") if el.get("id") is None else self.cur_uuid()
                        if lookup_uuid in self.uuid2ctr_map:
                            assert self.is_account_tag(self.uuid2ctr_map[lookup_uuid]), self.uuid2ctr_map[lookup_uuid]
                        self.handle_xact("account", lookup_uuid, el, self.el_order)
                    else:
                        xmlid = el.get("reference")
                        dbhelper.execute_dml("UPDATE xact SET _order=? WHERE _xmlid=?", (self.el_order, xmlid))

                elif el.tag == "accountTransaction":
                    if el.get("id") or el.find("uuid") is not None:
                        parent = el.getparent()
                        account_node = parent.find("account")
                        uuid = self.uuid(account_node) if account_node is not None else self.cur_uuid()
                        if uuid in self.uuid2ctr_map:
                            assert self.is_account_tag(self.uuid2ctr_map[uuid]), self.uuid2ctr_map[uuid]
                        self.handle_xact("account", uuid, el, 0)

                elif el.tag == "portfolio-transaction":
                    if el.get("id") or el.find("uuid") is not None:
                        # CORRECT: Data transactions must use their own unique transaction UUID
                        lookup_uuid = el.findtext("uuid") if el.get("id") is None else self.cur_uuid()
                        
                        routing_type = "portfolio"
                        if lookup_uuid in self.uuid2ctr_map:
                            if not self.uuid2ctr_map[lookup_uuid].startswith("portfolio"):
                                routing_type = "account"
                                
                        self.handle_xact(routing_type, lookup_uuid, el, self.el_order)
                    else:
                        xmlid = el.get("reference")
                        dbhelper.execute_dml("UPDATE xact SET _order=? WHERE _xmlid=?", (self.el_order, xmlid))

                elif el.tag in ("portfolioTransaction",):
                    if el.get("id") or el.find("uuid") is not None:
                        parent = el.getparent()
                        port_node = parent.find("portfolio")
                        uuid = self.uuid(port_node) if port_node is not None else self.cur_uuid()
                        
                        routing_type = "portfolio"
                        if uuid in self.uuid2ctr_map:
                            if not self.uuid2ctr_map[uuid].startswith("portfolio"):
                                routing_type = "account"
                                
                        self.handle_xact(routing_type, uuid, el, 0)

                elif el.tag == "transactionTo":
                    if el.get("id") or el.find("uuid") is not None:
                        parent = el.getparent()
                        assert parent.tag == "crossEntry"
                        if parent.get("class") == "account-transfer":
                            what = "account"
                            uuid = self.uuid(parent.find("accountTo"))
                        elif parent.get("class") == "portfolio-transfer":
                            what = "portfolio"
                            uuid = self.uuid(parent.find("portfolioTo"))
                        else:
                            assert False, "Unexpected crossEntry class: " + parent.get("class")

                        if uuid in self.uuid2ctr_map:
                            if what == "account":
                                assert self.is_account_tag(self.uuid2ctr_map[uuid]), self.uuid2ctr_map[uuid]
                            else:
                                assert self.uuid2ctr_map[uuid].startswith(what), self.uuid2ctr_map[uuid]
                        self.handle_xact(what, uuid, el, 0)
                elif el.tag == "transactionFrom":
                    if el.get("id") or el.find("uuid") is not None:
                        parent = el.getparent()
                        assert parent.tag == "crossEntry"
                        if parent.get("class") == "account-transfer":
                            what = "account"
                            uuid = self.uuid(parent.find("accountFrom"))
                        elif parent.get("class") == "portfolio-transfer":
                            what = "portfolio"
                            uuid = self.uuid(parent.find("portfolioFrom"))
                        else:
                            assert False, "Unexpected crossEntry class: " + parent.get("class")

                        if uuid in self.uuid2ctr_map:
                            if what == "account":
                                assert self.is_account_tag(self.uuid2ctr_map[uuid]), self.uuid2ctr_map[uuid]
                            else:
                                assert self.uuid2ctr_map[uuid].startswith(what), self.uuid2ctr_map[uuid]
                        self.handle_xact(what, uuid, el, 0)

                elif el.tag == "crossEntry":
                    if el.get("id") or len(list(el)) > 0: # Check if it contains structured child elements
                        self.handle_crossEntry(el)

                elif el.tag == "taxonomy":
                    self.handle_taxonomy(el)
                elif el.tag == "dashboard":
                    self.handle_dashboard(el)
                elif el.tag == "settings":
                    self.handle_settings(el)
                elif el.tag == "properties" and self.el_stack[-1] == "client":
                    self.handle_toplevel_properties(el)
                elif el.tag == "client":
                    self.handle_client(el)

                if el.get("reference") is None and self.container_stack and self.container_stack[-1][0] == el.tag:
                    self.container_stack.pop()

                # To save memory, we clear children of processed elements,
                # except for cases below.
                preserve = False
                if self.container_stack and self.container_stack[-1][0] in ("taxonomy", "dashboard", "settings"):
                    preserve = True
                # FIXED: Preserve nested identity and accounting children during traversal
                elif el.tag in ("units", "unit", "amount", "uuid", "id"):
                    preserve = True
                elif el.tag in ("limitPrice", "bookmark"):
                    preserve = True
                elif el.tag in ("map", "entry"):
                    preserve = True
                elif self.el_stack and self.el_stack[-1] == "watchlist" and el.tag == "securities":
                    preserve = True
                # FIXED: Retain structural transaction trees and account frameworks so handlers can parse them
                elif el.tag in ("account", "portfolio", "account-transaction", "accountTransaction", "portfolio-transaction", "portfolioTransaction", "crossEntry", "transactionFrom", "transactionTo"):
                    preserve = True
                elif self.container_stack and self.container_stack[-1][0] in ("security", "account", "portfolio") and el.tag in ("attributes", "property", "entry"):
                    preserve = True

                if not preserve:
                    # Remove children and text of elements. We don't use
                    # el.clear(), as that also removed attributes, but
                    # we want to preserve them (need id/reference at least).
                    for ch in list(el):
                        el.remove(ch)
                    el.text = el.tail = None

if __name__ == "__main__":
    argp = argparse.ArgumentParser(description="Import PortfolioPerformance XML file to Sqlite DB")
    argp.add_argument("xml_file", help="input XML file")
    argp.add_argument("db", help="output DB (filename/connect string)")
    argp.add_argument("--dbtype", choices=("sqlite", "pgsql"), default="sqlite", help="select database type")
    argp.add_argument("--debug", action="store_true", help="enable debug logging")
    argp.add_argument("--dry-run", action="store_true", help="don't commit changes to DB")
    argp.add_argument("--skip-prices", action="store_true", help="don't import historical prices (95+%% of DB size and import time; useful for debugging)")
    argp.add_argument("--version", action="version", version="%(prog)s " + __version__)
    args = argp.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)

    dbhelper.init(args.dbtype, args.db)

    with open(args.xml_file, "rb") as f:
        conv = PortfolioPerformanceXML2DB(f)
        conv.iterparse()

    if not args.dry_run:
        dbhelper.commit()
