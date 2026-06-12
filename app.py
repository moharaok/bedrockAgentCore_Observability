import os
import re
import time
import json
from flask import Flask, render_template, jsonify, request
import boto3

app = Flask(__name__)

# Load configuration
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

CONFIGURED_REGIONS = os.environ.get("CONFIGURED_REGIONS", "").split(",") if os.environ.get("CONFIGURED_REGIONS") else CONFIG.get("regions", ["us-east-1"])
LOG_GROUPS = CONFIG.get("log_groups", {})
PRICING_CONFIG = CONFIG.get("pricing", {})

# Build pricing lookup from config
BEDROCK_PRICING = {}
for model_id, prices in PRICING_CONFIG.get("models", {}).items():
    BEDROCK_PRICING[model_id] = {
        "input": prices["input_per_1k"],
        "output": prices["output_per_1k"],
    }


_CROSS_REGION_PREFIXES = ("us.", "global.", "eu.", "apac.", "us-gov.")


def _normalize_model_id(model_id):
    """Strip cross-region inference prefixes so 'us.anthropic.claude-...' matches 'anthropic.claude-...'."""
    for prefix in _CROSS_REGION_PREFIXES:
        if model_id.startswith(prefix):
            return model_id[len(prefix):]
    return model_id


def get_model_pricing(model_id):
    """Get pricing for a model. Tries exact match, then a normalized cross-region match."""
    if model_id in BEDROCK_PRICING:
        return BEDROCK_PRICING[model_id]

    # Try matching after stripping cross-region prefix on either side.
    normalized = _normalize_model_id(model_id)
    if normalized in BEDROCK_PRICING:
        return BEDROCK_PRICING[normalized]
    for key, pricing in BEDROCK_PRICING.items():
        if key == "_default":
            continue
        if _normalize_model_id(key) == normalized:
            return pricing

    return BEDROCK_PRICING.get("_default", {"input": 0.001, "output": 0.004})


@app.route("/")
def index():
    # If not configured yet, show settings page
    if not CONFIG.get("_is_configured", False):
        return render_template("settings.html")
    return render_template("landing.html")


@app.route("/agents")
def agents_page():
    return render_template("index.html")


@app.route("/costs")
def costs_page():
    return render_template("costs.html")


@app.route("/settings")
def settings():
    return render_template("settings.html")


@app.route("/api/regions")
def get_regions():
    return jsonify({"regions": CONFIGURED_REGIONS})


@app.route("/api/config")
def get_config():
    """Return non-sensitive config for the frontend."""
    return jsonify({
        "regions": CONFIGURED_REGIONS,
        "default_region": CONFIG.get("default_region", CONFIGURED_REGIONS[0] if CONFIGURED_REGIONS else "us-east-1"),
        "default_time_range": CONFIG.get("default_time_range", "86400"),
        "log_groups": LOG_GROUPS,
        "pricing_source": PRICING_CONFIG.get("source_url", ""),
        "pricing_last_updated": PRICING_CONFIG.get("last_updated", ""),
        "models_count": len(BEDROCK_PRICING),
        "is_configured": CONFIG.get("_is_configured", False),
    })


@app.route("/api/config", methods=["POST"])
def save_config():
    """Save configuration from the settings page."""
    try:
        new_config = request.json
        # Merge with existing config
        CONFIG["regions"] = new_config.get("regions", CONFIG.get("regions", []))
        CONFIG["default_region"] = new_config.get("default_region", CONFIG.get("default_region", ""))
        CONFIG["default_time_range"] = new_config.get("default_time_range", CONFIG.get("default_time_range", "86400"))
        CONFIG["log_groups"] = new_config.get("log_groups", CONFIG.get("log_groups", {}))
        if "pricing" in new_config:
            CONFIG["pricing"]["source_url"] = new_config["pricing"].get("source_url", CONFIG["pricing"].get("source_url", ""))
            CONFIG["pricing"]["last_updated"] = new_config["pricing"].get("last_updated", CONFIG["pricing"].get("last_updated", ""))
        CONFIG["_is_configured"] = True

        # Update runtime variables
        global CONFIGURED_REGIONS, LOG_GROUPS, PRICING_CONFIG
        CONFIGURED_REGIONS = CONFIG["regions"]
        LOG_GROUPS = CONFIG["log_groups"]
        PRICING_CONFIG = CONFIG["pricing"]

        # Save to file
        with open(CONFIG_PATH, "w") as f:
            json.dump(CONFIG, f, indent=2)

        return jsonify({"status": "ok", "message": "Configuration saved"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/pricing")
def get_pricing():
    """Return current pricing data."""
    return jsonify({
        "models": PRICING_CONFIG.get("models", {}),
        "source_url": PRICING_CONFIG.get("source_url", ""),
        "last_updated": PRICING_CONFIG.get("last_updated", ""),
    })


_USAGETYPE_REGION_PREFIX_RE = re.compile(r"^[A-Z]{2,4}\d?-")
_TOKEN_SUFFIX_RE = re.compile(
    r"-(?:Input|Output|Inp|Outp|InputTokens|OutputTokens|Inp-Tokens|Outp-Tokens|Input-Tokens|Output-Tokens)$",
    re.IGNORECASE,
)


def _classify_token_direction(*candidates):
    """Return 'input', 'output', or None given a bag of strings (group, usagetype, feature, description)."""
    blob = " ".join(c for c in candidates if c).lower()
    # Order matters: 'output' check first since 'input' substring can appear elsewhere ambiguously
    if "output" in blob or "outp-tokens" in blob or blob.endswith("-outp"):
        return "output"
    if "input" in blob or "inp-tokens" in blob or blob.endswith("-inp"):
        return "input"
    return None


def _extract_model_id_from_usagetype(usagetype):
    """Strip region prefix and Input/Output suffix from a usagetype to recover the bedrock model id."""
    if not usagetype:
        return ""
    stripped = _USAGETYPE_REGION_PREFIX_RE.sub("", usagetype, count=1)
    stripped = _TOKEN_SUFFIX_RE.sub("", stripped)
    return stripped


def _normalize_to_per_1k(price_per_unit, unit):
    """Convert a Pricing API price to per-1K tokens regardless of the source unit."""
    unit_lower = (unit or "").lower()
    # Common units: '1K tokens', 'tokens', '1000 tokens', '1M tokens'
    if "1m" in unit_lower or "1,000,000" in unit_lower or "million" in unit_lower:
        return price_per_unit / 1000.0
    if "1k" in unit_lower or "1000" in unit_lower or "1,000" in unit_lower:
        return price_per_unit
    if "token" in unit_lower:
        # Per-token pricing — scale up to per-1K
        return price_per_unit * 1000.0
    # Unknown unit; assume per-1K to avoid wildly wrong answers from a bad scale
    return price_per_unit


@app.route("/api/pricing/refresh", methods=["POST"])
def refresh_pricing():
    """Fetch latest pricing from AWS Price List API and update config."""
    try:
        # AWS Pricing API is only available in us-east-1 / ap-south-1
        pricing_client = boto3.client("pricing", region_name="us-east-1")

        updated_models = {}
        items_seen = 0
        items_classified = 0
        next_token = None

        while True:
            params = {"ServiceCode": "AmazonBedrock", "MaxResults": 100}
            if next_token:
                params["NextToken"] = next_token

            response = pricing_client.get_products(**params)

            for price_item_str in response.get("PriceList", []):
                items_seen += 1
                try:
                    price_item = json.loads(price_item_str) if isinstance(price_item_str, str) else price_item_str
                except (json.JSONDecodeError, TypeError):
                    continue

                product = price_item.get("product", {})
                attributes = product.get("attributes", {})
                terms = price_item.get("terms", {}).get("OnDemand", {})

                usagetype = attributes.get("usagetype", "")
                group = attributes.get("group", "")
                feature = attributes.get("feature", "")
                description = attributes.get("usageDescription", "") or attributes.get("operation", "")
                region_code = attributes.get("regionCode", "")

                direction = _classify_token_direction(group, usagetype, feature, description)
                if direction is None:
                    continue
                # Only us-east-1 anchor pricing — Bedrock on-demand pricing is region-keyed
                if region_code and region_code != "us-east-1":
                    continue

                # Prefer explicit model attributes, fall back to parsing usagetype.
                model_id = (
                    attributes.get("model")
                    or attributes.get("modelId")
                    or _extract_model_id_from_usagetype(usagetype)
                )
                if not model_id:
                    continue

                for term_val in terms.values():
                    for dim_val in term_val.get("priceDimensions", {}).values():
                        try:
                            price_per_unit = float(dim_val.get("pricePerUnit", {}).get("USD", "0"))
                        except (TypeError, ValueError):
                            continue
                        if price_per_unit <= 0:
                            continue

                        price_per_1k = _normalize_to_per_1k(price_per_unit, dim_val.get("unit", ""))
                        slot = updated_models.setdefault(model_id, {})
                        slot[f"{direction}_per_1k"] = price_per_1k
                        items_classified += 1

            next_token = response.get("NextToken")
            if not next_token:
                break

        # Merge: only adopt entries that have BOTH input and output prices.
        existing_models = PRICING_CONFIG.get("models", {})
        merged_count = 0
        for model_id, prices in updated_models.items():
            if "input_per_1k" in prices and "output_per_1k" in prices:
                existing_models[model_id] = prices
                merged_count += 1

        from datetime import date
        PRICING_CONFIG["models"] = existing_models
        PRICING_CONFIG["last_updated"] = date.today().isoformat()
        CONFIG["pricing"] = PRICING_CONFIG

        global BEDROCK_PRICING
        BEDROCK_PRICING = {
            mid: {"input": p["input_per_1k"], "output": p["output_per_1k"]}
            for mid, p in existing_models.items()
            if "input_per_1k" in p and "output_per_1k" in p
        }

        with open(CONFIG_PATH, "w") as f:
            json.dump(CONFIG, f, indent=2)

        return jsonify({
            "status": "ok",
            "message": (
                f"Pricing refresh complete. Scanned {items_seen} price items, "
                f"classified {items_classified} input/output entries, "
                f"merged {merged_count} models with full pricing. "
                f"{len(existing_models)} total models in config."
            ),
            "items_seen": items_seen,
            "items_classified": items_classified,
            "models_merged": merged_count,
            "last_updated": PRICING_CONFIG["last_updated"],
            "models_count": len(existing_models),
        })
    except Exception as e:
        return jsonify({"error": str(e), "status": "error"}), 500


@app.route("/api/pricing/model", methods=["POST"])
def update_model_pricing():
    """Manually update pricing for a single model."""
    try:
        data = request.json
        model_id = data.get("model_id", "")
        input_per_1k = float(data.get("input_per_1k", 0))
        output_per_1k = float(data.get("output_per_1k", 0))

        if not model_id:
            return jsonify({"error": "model_id is required"}), 400

        # Update config
        if "models" not in PRICING_CONFIG:
            PRICING_CONFIG["models"] = {}
        PRICING_CONFIG["models"][model_id] = {
            "input_per_1k": input_per_1k,
            "output_per_1k": output_per_1k,
        }
        CONFIG["pricing"] = PRICING_CONFIG

        # Update runtime pricing
        global BEDROCK_PRICING
        BEDROCK_PRICING[model_id] = {"input": input_per_1k, "output": output_per_1k}

        # Save to file
        with open(CONFIG_PATH, "w") as f:
            json.dump(CONFIG, f, indent=2)

        return jsonify({"status": "ok", "message": f"Pricing updated for {model_id}"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/pricing/model/<path:model_id>", methods=["DELETE"])
def delete_model_pricing(model_id):
    """Delete pricing for a model."""
    try:
        if model_id in PRICING_CONFIG.get("models", {}):
            del PRICING_CONFIG["models"][model_id]
            CONFIG["pricing"] = PRICING_CONFIG

            global BEDROCK_PRICING
            BEDROCK_PRICING.pop(model_id, None)

            with open(CONFIG_PATH, "w") as f:
                json.dump(CONFIG, f, indent=2)

        return jsonify({"status": "ok", "message": f"Deleted pricing for {model_id}"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/summary")
def get_summary():
    """Aggregate metrics across all agents for the landing page."""
    seconds = int(request.args.get("seconds", 86400))  # Default: last 24h
    region = request.args.get("region", CONFIGURED_REGIONS[0])
    end_time = int(time.time())
    start_time = end_time - seconds

    try:
        client = boto3.client("logs", region_name=region)

        # Query aws/spans for aggregate metrics
        query = """
            fields @message
            | parse @message /"aws.local.service":"(?<service>[^"]+)"/
            | parse @message /"aws.span.kind":"(?<spanKind>[^"]+)"/
            | parse @message /"gen_ai.request.model":"(?<model>[^"]+)"/
            | parse @message /"gen_ai.usage.input_tokens":(?<inputTokens>\\d+)/
            | parse @message /"gen_ai.usage.output_tokens":(?<outputTokens>\\d+)/
            | parse @message /"session.id":"(?<sessionId>[^"]+)"/
            | parse @message /"name":"(?<spanName>[^"]+)"/
            | filter spanKind = 'LOCAL_ROOT' or @message like /invoke_agent/
            | stats
                count(*) as totalSpans,
                count_distinct(service) as uniqueAgents,
                count_distinct(sessionId) as uniqueSessions,
                sum(inputTokens) as totalInputTokens,
                sum(outputTokens) as totalOutputTokens
            by service, model
        """.strip()

        response = client.start_query(
            logGroupNames=[LOG_GROUPS["spans"]],
            startTime=start_time,
            endTime=end_time,
            queryString=query,
        )
        query_id = response["queryId"]

        status = "Running"
        results = []
        while status in ("Running", "Scheduled"):
            time.sleep(1)
            result = client.get_query_results(queryId=query_id)
            status = result.get("status", "Unknown")
            results = result.get("results", [])

        # Also get total agents count
        all_agents = []
        try:
            ctrl = boto3.client("bedrock-agentcore-control", region_name=region)
            paginator = ctrl.get_paginator("list_agent_runtimes")
            for page in paginator.paginate():
                for agent in page.get("agentRuntimes", []):
                    all_agents.append({
                        "name": agent.get("agentRuntimeName", ""),
                        "status": agent.get("status", ""),
                        "region": region,
                    })
        except Exception:
            pass

        # Process span results into summary. Each row is grouped by (service, model),
        # so we cost each row with its own model's pricing — no per-agent pricing fudge.
        total_input_tokens = 0
        total_output_tokens = 0
        total_sessions = set()
        models_usage = {}
        agents_activity = {}
        cost_by_agent = {}

        for row in results:
            record = {}
            for field in row:
                if field["field"] and field["value"]:
                    record[field["field"]] = field["value"]

            service = record.get("service", "")
            model = record.get("model", "")
            input_tokens = int(record.get("totalInputTokens", 0) or 0)
            output_tokens = int(record.get("totalOutputTokens", 0) or 0)
            spans = int(record.get("totalSpans", 0) or 0)

            total_input_tokens += input_tokens
            total_output_tokens += output_tokens

            row_pricing = get_model_pricing(model) if model else BEDROCK_PRICING.get(
                "_default", {"input": 0.001, "output": 0.004}
            )
            row_input_cost = (input_tokens / 1000) * row_pricing["input"]
            row_output_cost = (output_tokens / 1000) * row_pricing["output"]
            row_cost = row_input_cost + row_output_cost

            if model:
                bucket = models_usage.setdefault(
                    model,
                    {"input_tokens": 0, "output_tokens": 0, "calls": 0, "input_cost": 0, "output_cost": 0},
                )
                bucket["input_tokens"] += input_tokens
                bucket["output_tokens"] += output_tokens
                bucket["calls"] += spans
                bucket["input_cost"] += row_input_cost
                bucket["output_cost"] += row_output_cost

            if service:
                agent_name = service.replace(".DEFAULT", "")
                activity = agents_activity.setdefault(
                    agent_name,
                    {"spans": 0, "input_tokens": 0, "output_tokens": 0, "models": set()},
                )
                activity["spans"] += spans
                activity["input_tokens"] += input_tokens
                activity["output_tokens"] += output_tokens
                if model:
                    activity["models"].add(model)

                agent_cost = cost_by_agent.setdefault(
                    agent_name,
                    {"models": set(), "input_tokens": 0, "output_tokens": 0,
                     "input_cost": 0, "output_cost": 0, "total_cost": 0},
                )
                if model:
                    agent_cost["models"].add(model)
                agent_cost["input_tokens"] += input_tokens
                agent_cost["output_tokens"] += output_tokens
                agent_cost["input_cost"] += row_input_cost
                agent_cost["output_cost"] += row_output_cost
                agent_cost["total_cost"] += row_cost

        # Finalize per-model cost rollups
        total_cost = 0
        cost_by_model = {}
        for model, usage in models_usage.items():
            model_cost = usage["input_cost"] + usage["output_cost"]
            total_cost += model_cost
            cost_by_model[model] = {
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "calls": usage["calls"],
                "input_cost": round(usage["input_cost"], 6),
                "output_cost": round(usage["output_cost"], 6),
                "total_cost": round(model_cost, 6),
                "pricing": get_model_pricing(model),
            }

        # Finalize per-agent costs (sets → lists for JSON, round numbers)
        for agent_name, agent_cost in cost_by_agent.items():
            agent_cost["models"] = sorted(agent_cost["models"])
            agent_cost["input_cost"] = round(agent_cost["input_cost"], 6)
            agent_cost["output_cost"] = round(agent_cost["output_cost"], 6)
            agent_cost["total_cost"] = round(agent_cost["total_cost"], 6)

        for activity in agents_activity.values():
            activity["models"] = sorted(activity["models"])

        # Separate agents by type (MCP vs HTTP)
        mcp_agents = [a for a in all_agents if any(k in a["name"].lower() for k in ["mcp", "server"])]
        http_agents = [a for a in all_agents if a not in mcp_agents]

        summary = {
            "agents": {
                "total": len(all_agents),
                "mcp_servers": len(mcp_agents),
                "http_agents": len(http_agents),
                "active": len(agents_activity),
                "by_region": {},
            },
            "tokens": {
                "total_input": total_input_tokens,
                "total_output": total_output_tokens,
                "total": total_input_tokens + total_output_tokens,
            },
            "models": {
                "unique_count": len(models_usage),
                "by_model": cost_by_model,
            },
            "cost": {
                "total_usd": round(total_cost, 4),
                "by_model": {m: v["total_cost"] for m, v in cost_by_model.items()},
                "pricing_source": PRICING_CONFIG.get("source_url", ""),
            },
            "activity": {
                "active_agents": agents_activity,
                "cost_by_agent": cost_by_agent,
                "time_range_seconds": seconds,
            },
        }

        # Count by region
        for a in all_agents:
            r = a["region"]
            summary["agents"]["by_region"][r] = summary["agents"]["by_region"].get(r, 0) + 1

        return jsonify({"summary": summary})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/costs")
def get_costs():
    """Get detailed cost breakdown with multiple dimensions."""
    seconds = int(request.args.get("seconds", 86400))
    region = request.args.get("region", CONFIGURED_REGIONS[0])
    end_time = int(time.time())
    start_time = end_time - seconds

    try:
        client = boto3.client("logs", region_name=region)

        # Query spans with all dimensions we need
        query = """
            fields @message
            | parse @message /"aws.local.service":"(?<service>[^"]+)"/
            | parse @message /"gen_ai.request.model":"(?<model>[^"]+)"/
            | parse @message /"gen_ai.usage.input_tokens":(?<inputTokens>\\d+)/
            | parse @message /"gen_ai.usage.output_tokens":(?<outputTokens>\\d+)/
            | parse @message /"session.id":"(?<sessionId>[^"]+)"/
            | parse @message /"name":"invoke_agent(?<isInvoke>[^"]*)/
            | filter ispresent(isInvoke)
            | filter ispresent(model)
            | stats sum(inputTokens) as totalIn, sum(outputTokens) as totalOut, count(*) as calls by service, model, sessionId
        """.strip()

        response = client.start_query(
            logGroupNames=[LOG_GROUPS["spans"]],
            startTime=start_time,
            endTime=end_time,
            queryString=query,
        )
        query_id = response["queryId"]

        status = "Running"
        results = []
        while status in ("Running", "Scheduled"):
            time.sleep(1)
            result = client.get_query_results(queryId=query_id)
            status = result.get("status", "Unknown")
            results = result.get("results", [])

        # Parse results into records
        records = []
        for row in results:
            record = {}
            for field in row:
                if field["field"] and field["value"]:
                    record[field["field"]] = field["value"]
            records.append(record)

        # Build multi-dimensional cost data
        by_agent = {}
        by_model = {}
        by_region = {}
        by_session_dimension = {}  # Split session_id by "_"
        by_session = {}
        all_dimensions = set()

        for r in records:
            service = r.get("service", "").replace(".DEFAULT", "")
            model = r.get("model", "")
            session_id = r.get("sessionId", "")
            input_tokens = int(r.get("totalIn", 0) or 0)
            output_tokens = int(r.get("totalOut", 0) or 0)
            calls = int(r.get("calls", 0) or 0)

            pricing = get_model_pricing(model)
            input_cost = (input_tokens / 1000) * pricing["input"]
            output_cost = (output_tokens / 1000) * pricing["output"]
            total_cost = input_cost + output_cost

            cost_entry = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "calls": calls,
                "cost": round(total_cost, 6),
            }

            # By Agent
            if service not in by_agent:
                by_agent[service] = {"input_tokens": 0, "output_tokens": 0, "calls": 0, "cost": 0, "models": set(), "sessions": set(), "dimensions": {}}
            by_agent[service]["input_tokens"] += input_tokens
            by_agent[service]["output_tokens"] += output_tokens
            by_agent[service]["calls"] += calls
            by_agent[service]["cost"] += total_cost
            if model:
                by_agent[service]["models"].add(model)
            if session_id:
                by_agent[service]["sessions"].add(session_id)
                # Track session dimensions per agent
                if "_" in session_id:
                    parts = session_id.split("_")
                    # Extract all parts before the UUID as dimensions (dim_1, dim_2, etc.)
                    dim_parts = [p for p in parts if not is_uuid_part(p)]
                    for idx, dim_value in enumerate(dim_parts):
                        dim_name = f"dim_{idx + 1}"
                        if dim_value:
                            if dim_name not in by_agent[service]["dimensions"]:
                                by_agent[service]["dimensions"][dim_name] = {}
                            if dim_value not in by_agent[service]["dimensions"][dim_name]:
                                by_agent[service]["dimensions"][dim_name][dim_value] = {"cost": 0, "calls": 0}
                            by_agent[service]["dimensions"][dim_name][dim_value]["cost"] += total_cost
                            by_agent[service]["dimensions"][dim_name][dim_value]["calls"] += calls

            # By Model
            if model not in by_model:
                by_model[model] = {"input_tokens": 0, "output_tokens": 0, "calls": 0, "cost": 0, "agents": set(), "pricing": pricing}
            by_model[model]["input_tokens"] += input_tokens
            by_model[model]["output_tokens"] += output_tokens
            by_model[model]["calls"] += calls
            by_model[model]["cost"] += total_cost
            by_model[model]["agents"].add(service)

            # By Region (all data is from the selected region)
            query_region = region
            if query_region not in by_region:
                by_region[query_region] = {"input_tokens": 0, "output_tokens": 0, "calls": 0, "cost": 0, "agents": set()}
            by_region[query_region]["input_tokens"] += input_tokens
            by_region[query_region]["output_tokens"] += output_tokens
            by_region[query_region]["calls"] += calls
            by_region[query_region]["cost"] += total_cost
            by_region[region]["agents"].add(service)

            # By Session (full)
            if session_id:
                if session_id not in by_session:
                    by_session[session_id] = {"input_tokens": 0, "output_tokens": 0, "calls": 0, "cost": 0, "agent": service, "models": set()}
                by_session[session_id]["input_tokens"] += input_tokens
                by_session[session_id]["output_tokens"] += output_tokens
                by_session[session_id]["calls"] += calls
                by_session[session_id]["cost"] += total_cost
                if model:
                    by_session[session_id]["models"].add(model)

            # By Session Dimension (split session_id by "_")
            if session_id and "_" in session_id:
                parts = session_id.split("_")
                # Extract all non-UUID parts as dimensions
                dim_parts = [p for p in parts if not is_uuid_part(p)]
                for idx, dim_value in enumerate(dim_parts):
                    dim_name = f"dim_{idx + 1}"
                    if dim_value:
                        key = f"{dim_name}:{dim_value}"
                        all_dimensions.add(dim_name)
                        if key not in by_session_dimension:
                            by_session_dimension[key] = {"input_tokens": 0, "output_tokens": 0, "calls": 0, "cost": 0, "sessions": 0, "dimension": dim_name, "value": dim_value, "agents": set(), "models": set()}
                        by_session_dimension[key]["input_tokens"] += input_tokens
                        by_session_dimension[key]["output_tokens"] += output_tokens
                        by_session_dimension[key]["calls"] += calls
                        by_session_dimension[key]["cost"] += total_cost
                        by_session_dimension[key]["sessions"] += 1
                        if service:
                            by_session_dimension[key]["agents"].add(service)
                        if model:
                            by_session_dimension[key]["models"].add(model)

        # Convert sets to lists for JSON
        for v in by_agent.values():
            v["models"] = list(v.get("models", set()))
            v["sessions"] = len(v.get("sessions", set()))
            v["cost"] = round(v["cost"], 6)
            # Round dimension costs
            for dim_name, dim_values in v.get("dimensions", {}).items():
                for dv in dim_values.values():
                    dv["cost"] = round(dv["cost"], 6)
        for v in by_model.values():
            v["agents"] = list(v.get("agents", set()))
            v["cost"] = round(v["cost"], 6)
        for v in by_region.values():
            v["agents"] = list(v.get("agents", set()))
            v["cost"] = round(v["cost"], 6)
        for v in by_session.values():
            v["cost"] = round(v["cost"], 6)
            v["models"] = list(v.get("models", set()))
        for v in by_session_dimension.values():
            v["cost"] = round(v["cost"], 6)
            v["agents"] = list(v.get("agents", set()))
            v["models"] = list(v.get("models", set()))

        # Sort sessions by cost descending, limit top 50
        top_sessions = dict(sorted(by_session.items(), key=lambda x: x[1]["cost"], reverse=True)[:50])

        total_cost = sum(v["cost"] for v in by_agent.values())

        return jsonify({
            "total_cost": round(total_cost, 4),
            "by_agent": by_agent,
            "by_model": by_model,
            "by_region": by_region,
            "by_session_dimension": by_session_dimension,
            "by_session": top_sessions,
            "dimensions_available": list(all_dimensions),
            "time_range_seconds": seconds,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def is_uuid_part(s):
    """Check if a string looks like a UUID segment."""
    import re
    return bool(re.match(r'^[a-f0-9]{4,}(-[a-f0-9]+)*$', s))


@app.route("/api/agents")
def list_agents():
    region = request.args.get("region", CONFIGURED_REGIONS[0])
    all_agents = []
    try:
        client = boto3.client("bedrock-agentcore-control", region_name=region)
        paginator = client.get_paginator("list_agent_runtimes")
        for page in paginator.paginate():
            for agent in page.get("agentRuntimes", []):
                agent["region"] = region
                # Convert datetime to string
                if "lastUpdatedAt" in agent:
                    agent["lastUpdatedAt"] = agent["lastUpdatedAt"].isoformat()
                if "createdAt" in agent:
                    agent["createdAt"] = agent["createdAt"].isoformat()
                all_agents.append(agent)
    except Exception as e:
        print(f"Error listing agents in {region}: {e}")
    return jsonify({"agents": all_agents})


@app.route("/api/agents/<agent_id>/detail")
def get_agent_detail(agent_id):
    region = request.args.get("region", "us-east-1")
    try:
        client = boto3.client("bedrock-agentcore-control", region_name=region)
        result = client.get_agent_runtime(agentRuntimeId=agent_id)
        # Remove ResponseMetadata
        result.pop("ResponseMetadata", None)
        # Convert datetime fields
        for key in ["createdAt", "lastUpdatedAt"]:
            if key in result and hasattr(result[key], "isoformat"):
                result[key] = result[key].isoformat()
        return jsonify({"agent": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sessions")
def get_sessions():
    region = request.args.get("region", "us-east-1")
    start_time = int(request.args.get("startTime", int(time.time()) - 86400))
    end_time = int(request.args.get("endTime", int(time.time())))
    agent_name = request.args.get("agentName", "")

    try:
        client = boto3.client("logs", region_name=region)

        filter_clause = ""
        if agent_name:
            filter_clause = f'| filter @message like /{agent_name}/'

        query = f"""
            fields @message
            | parse @message /\"aws.local.service\":\"(?<service>[^\"]+)\"/
            | parse @message /\"session.id\":\"(?<sessionId>[^\"]+)\"/
            | parse @message /\"aws.span.kind\":\"(?<spanKind>[^\"]+)\"/
            | filter (spanKind = 'LOCAL_ROOT' or @message like /"name":"invoke_agent/)
            | filter ispresent(service)
            | filter ispresent(sessionId)
            {filter_clause}
            | stats count(*) as spanCount, min(@timestamp) as firstSeen, max(@timestamp) as lastSeen by service, sessionId
            | sort lastSeen desc
            | limit 200
        """.strip()

        response = client.start_query(
            logGroupNames=[LOG_GROUPS["spans"]],
            startTime=start_time,
            endTime=end_time,
            queryString=query,
        )
        query_id = response["queryId"]

        # Poll for results
        status = "Running"
        results = []
        while status in ("Running", "Scheduled"):
            time.sleep(1)
            result = client.get_query_results(queryId=query_id)
            status = result.get("status", "Unknown")
            results = result.get("results", [])

        # Transform results
        sessions = []
        for row in results:
            record = {}
            for field in row:
                record[field["field"]] = field["value"]
            sessions.append(record)

        return jsonify({"sessions": sessions})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sessions/<session_id>/conversation")
def get_session_conversation(session_id):
    """Load full conversation as a clean step-by-step narrative."""
    region = request.args.get("region", "us-east-1")
    agent_id = request.args.get("agentId", "")
    start_time = int(request.args.get("startTime", int(time.time()) - 2592000))
    end_time = int(request.args.get("endTime", int(time.time())))

    try:
        # Extract actor_id from session_id pattern
        # Some session IDs use format "actorId_sessionUUID", others are plain UUIDs
        actor_id = session_id.split("_")[0] if "_" in session_id else session_id

        # Get memory_id from agent's environment variables
        memory_id = request.args.get("memoryId", "")
        if not memory_id and agent_id:
            try:
                control_client = boto3.client("bedrock-agentcore-control", region_name=region)
                agent_detail = control_client.get_agent_runtime(agentRuntimeId=agent_id)
                env_vars = agent_detail.get("environmentVariables", {})
                memory_id = env_vars.get("BEDROCK_AGENTCORE_MEMORY_ID", "") or env_vars.get("MEMORY_ID", "")
            except Exception:
                pass

        # Load raw spans
        spans = load_raw_spans(region, session_id, start_time, end_time)

        # Load events (actual messages) if memory available
        event_messages = []
        if memory_id and actor_id:
            event_messages = load_conversation_from_events(region, memory_id, actor_id, session_id)

        # Always try agent runtime logs for the actual conversation content
        # (spans only give us metadata like model, tokens, duration - not the messages)
        runtime_conversation = load_conversation_from_runtime_logs(
            region, agent_id, session_id, start_time, end_time
        )

        view = request.args.get("view", "actor_flow")  # "actor_flow" | "messages" | "legacy"

        if view == "actor_flow" and spans:
            conversation = build_actor_flow_narrative(spans, runtime_conversation)
            # If span data was thin and the flow is empty, fall back to message view
            if not conversation:
                conversation = runtime_conversation or build_clean_narrative(spans, event_messages)
        elif view == "messages" and runtime_conversation:
            conversation = runtime_conversation
        else:
            conversation = build_clean_narrative(spans, event_messages)
            if runtime_conversation:
                has_content = any(
                    s.get("style") in ("user", "assistant") for s in runtime_conversation
                )
                if has_content:
                    conversation = runtime_conversation

        return jsonify({
            "conversation": conversation,
            "sessionId": session_id,
            "actorId": actor_id,
            "memoryId": memory_id,
            "view": view,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def load_raw_spans(region, session_id, start_time, end_time):
    """Load and parse all spans for a session."""
    client = boto3.client("logs", region_name=region)
    query = f"""
        fields @timestamp, @message
        | filter @message like /"{session_id}"/
        | sort @timestamp asc
        | limit 1000
    """.strip()

    response = client.start_query(
        logGroupNames=[LOG_GROUPS["spans"]],
        startTime=start_time,
        endTime=end_time,
        queryString=query,
    )
    query_id = response["queryId"]

    status = "Running"
    results = []
    while status in ("Running", "Scheduled"):
        time.sleep(1)
        result = client.get_query_results(queryId=query_id)
        status = result.get("status", "Unknown")
        results = result.get("results", [])

    spans = []
    for row in results:
        msg_text = ""
        timestamp = ""
        for field in row:
            if field["field"] == "@message":
                msg_text = field["value"]
            elif field["field"] == "@timestamp":
                timestamp = field["value"]
        if msg_text:
            try:
                span = json.loads(msg_text)
                span["_timestamp"] = timestamp
                spans.append(span)
            except json.JSONDecodeError:
                continue

    spans.sort(key=lambda s: s.get("startTimeUnixNano", 0))
    return spans


def load_conversation_from_runtime_logs(region, agent_id, session_id, start_time, end_time):
    """Load conversation from agent runtime log group.
    
    Agent runtime logs are stored in: /aws/bedrock-agentcore/runtimes/<agentId>-DEFAULT
    These contain OTEL-formatted logs with the actual user inputs, agent responses,
    tool calls, and conversation flow emitted by Strands SDK during execution.
    """
    if not agent_id:
        print(f"[runtime_logs] No agent_id provided, skipping")
        return []

    runtime_prefix = LOG_GROUPS.get("runtime_prefix", "/aws/bedrock-agentcore/runtimes")
    # Runtime log groups have -DEFAULT appended
    log_group = f"{runtime_prefix}/{agent_id}-DEFAULT"
    print(f"[runtime_logs] Querying log group: {log_group} for session: {session_id}")

    client = boto3.client("logs", region_name=region)

    # Query for log entries matching this session
    query = f"""
        fields @timestamp, @message
        | filter @message like /"{session_id}"/
        | sort @timestamp asc
        | limit 100
    """.strip()

    try:
        response = client.start_query(
            logGroupNames=[log_group],
            startTime=start_time,
            endTime=end_time,
            queryString=query.strip(),
        )
        query_id = response["queryId"]

        status = "Running"
        results = []
        while status in ("Running", "Scheduled"):
            time.sleep(1)
            result = client.get_query_results(queryId=query_id)
            status = result.get("status", "Unknown")
            results = result.get("results", [])

        if not results:
            # Try without -DEFAULT suffix as fallback
            log_group_alt = f"{runtime_prefix}/{agent_id}"
            try:
                response = client.start_query(
                    logGroupNames=[log_group_alt],
                    startTime=start_time,
                    endTime=end_time,
                    queryString=query.strip(),
                )
                query_id = response["queryId"]

                status = "Running"
                while status in ("Running", "Scheduled"):
                    time.sleep(1)
                    result = client.get_query_results(queryId=query_id)
                    status = result.get("status", "Unknown")
                    results = result.get("results", [])
            except Exception:
                pass

        if not results:
            print(f"[runtime_logs] No results found in {log_group}")
            return []

        # Parse the OTEL runtime log entries into a conversation
        print(f"[runtime_logs] Found {len(results)} log entries, parsing...")
        conversation = parse_otel_runtime_logs(results)
        print(f"[runtime_logs] Parsed into {len(conversation)} conversation steps")
        return conversation

    except Exception as e:
        print(f"[runtime_logs] Error loading runtime logs for agent {agent_id}: {e}")
        return []



def parse_otel_runtime_logs(results):
    """Parse OTEL-formatted runtime logs into a conversation timeline.
    
    These logs have structure:
    {
      "body": {
        "input": {"messages": [{"role": "user/system/tool", "content": {...}}]},
        "output": {"messages": [{"role": "assistant", "content": {...}}]}
      },
      "attributes": {"session.id": "...", "event.name": "..."},
      "timeUnixNano": ...
    }
    """
    conversation = []
    seen_messages = set()  # Deduplicate messages across log entries

    for row in results:
        timestamp = ""
        message = ""
        for field in row:
            if field["field"] == "@timestamp":
                timestamp = field["value"]
            elif field["field"] == "@message":
                message = field["value"]

        if not message:
            continue

        try:
            log_entry = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            continue

        # Handle simple INFO logs (e.g., "Invocation completed successfully")
        if "body" not in log_entry and "message" in log_entry:
            msg = log_entry.get("message", "")
            if msg and "completed" in msg.lower():
                conversation.append({
                    "timestamp": timestamp,
                    "step": "system",
                    "title": "System",
                    "content": msg,
                    "icon": "\u2705",
                    "style": "log",
                })
            continue

        body = log_entry.get("body", {})
        if not body:
            continue

        input_msgs = body.get("input", {}).get("messages", [])
        output_msgs = body.get("output", {}).get("messages", [])

        # Extract user/system messages from input
        for msg in input_msgs:
            role = msg.get("role", "")
            content_raw = msg.get("content", {})
            text = _extract_message_text(content_raw)

            if not text:
                continue

            if role == "system":
                msg_key = f"system:{text[:100]}"
                if msg_key not in seen_messages:
                    seen_messages.add(msg_key)
                    conversation.append({
                        "timestamp": timestamp,
                        "step": "system",
                        "title": "System Prompt",
                        "content": text,
                        "icon": "\U0001f4dc",
                        "style": "system_prompt",
                    })
            elif role == "user":
                msg_key = f"user:{text[:100]}"
                if msg_key not in seen_messages:
                    seen_messages.add(msg_key)
                    conversation.append({
                        "timestamp": timestamp,
                        "step": "user",
                        "title": "User",
                        "content": text,
                        "icon": "\U0001f464",
                        "style": "user",
                    })
            elif role == "tool":
                # role=tool input messages come in two shapes:
                #   Shape A — tool call echo (replay of the LLM's prior toolUse as context):
                #       content.content is the INPUT JSON of the call, content.id is set.
                #       The tool_call entry already shows this input — skip to avoid mislabeling.
                #   Shape B — actual tool result: content.content is a JSON array of
                #       toolResult blocks. Render each, deduped by toolUseId.
                inner = msg.get("content", {})
                inner_payload = inner.get("content", "") if isinstance(inner, dict) else ""

                tool_blocks = []
                if isinstance(inner_payload, str):
                    try:
                        parsed = json.loads(inner_payload)
                        if isinstance(parsed, list):
                            tool_blocks = [b for b in parsed if isinstance(b, dict) and "toolResult" in b]
                    except (json.JSONDecodeError, TypeError):
                        pass

                if not tool_blocks:
                    # Shape A or unrecognized — drop, the tool_call carries the input already
                    continue

                for block in tool_blocks:
                    tr = block["toolResult"]
                    tool_use_id = tr.get("toolUseId", "")
                    msg_key = f"tool_result:{tool_use_id}" if tool_use_id else f"tool_result:{json.dumps(tr)[:200]}"
                    if msg_key in seen_messages:
                        continue
                    seen_messages.add(msg_key)

                    text_parts = []
                    for c in tr.get("content", []):
                        if isinstance(c, dict) and "text" in c:
                            text_parts.append(c["text"])
                    result_text = "\n".join(text_parts) if text_parts else json.dumps(tr.get("content", ""), indent=2)
                    # Pretty-print JSON if possible
                    try:
                        result_text = json.dumps(json.loads(result_text), indent=2)
                    except (json.JSONDecodeError, TypeError):
                        pass

                    status_str = tr.get("status", "success")
                    icon = "✅" if status_str == "success" else "❌"
                    conversation.append({
                        "timestamp": timestamp,
                        "step": "tool_result",
                        "title": f"Tool Result ({status_str})",
                        "content": result_text,
                        "icon": icon,
                        "style": "tool_result" if status_str == "success" else "error",
                        "tool_use_id": tool_use_id,
                    })

        # Extract assistant responses from output
        for msg in output_msgs:
            role = msg.get("role", "")
            content_raw = msg.get("content", {})

            if role != "assistant":
                continue

            if isinstance(content_raw, dict):
                message_text = content_raw.get("message", "")
                finish_reason = content_raw.get("finish_reason", "")

                if message_text:
                    parsed = _parse_content_blocks(message_text)
                    for block in parsed:
                        if block["type"] == "text":
                            # Suppress assistant "text" blocks that are actually JSON
                            # echoes of prior tool results / toolResult arrays. Real
                            # agent prose is plain text, never starts with { or [.
                            stripped = block["text"].lstrip()
                            if stripped.startswith(("{", "[")):
                                continue
                            msg_key = f"assistant:{block['text'][:200]}"
                            if msg_key not in seen_messages:
                                seen_messages.add(msg_key)
                                conversation.append({
                                    "timestamp": timestamp,
                                    "step": "assistant",
                                    "title": "Agent",
                                    "content": block["text"],
                                    "icon": "🤖",
                                    "style": "assistant",
                                })
                        elif block["type"] == "tool_use":
                            tool_use_id = block.get("toolUseId", "")
                            if tool_use_id:
                                msg_key = f"tool_call:{tool_use_id}"
                            else:
                                msg_key = f"tool_call:{block['name']}:{str(block.get('input', ''))[:50]}"
                            if msg_key not in seen_messages:
                                seen_messages.add(msg_key)
                                tool_input = block.get("input", "")
                                if isinstance(tool_input, dict):
                                    tool_input = json.dumps(tool_input, indent=2)
                                conversation.append({
                                    "timestamp": timestamp,
                                    "step": "tool_call",
                                    "title": f"Tool: {block['name']}",
                                    "content": f"**{block['name']}**\n```json\n{tool_input}\n```",
                                    "icon": "🔧",
                                    "style": "tool_call",
                                    "tool_use_id": tool_use_id,
                                    "tool_name": block.get("name", ""),
                                })

                # Check for tool.result in content (some SDKs put results on the assistant turn)
                tool_result_text = content_raw.get("tool.result", "")
                if tool_result_text:
                    parsed_results = _parse_content_blocks(tool_result_text)
                    for block in parsed_results:
                        if block["type"] == "tool_result":
                            tool_use_id = block.get("toolUseId", "")
                            msg_key = (
                                f"tool_result:{tool_use_id}" if tool_use_id
                                else f"tool_result:{block.get('text', '')[:200]}"
                            )
                            if msg_key not in seen_messages:
                                seen_messages.add(msg_key)
                                status_str = block.get("status", "success")
                                icon = "\u2705" if status_str == "success" else "\u274c"
                                # Pretty-print JSON results when possible
                                result_text = block.get("text", "")
                                try:
                                    result_text = json.dumps(json.loads(result_text), indent=2)
                                except (json.JSONDecodeError, TypeError):
                                    pass
                                conversation.append({
                                    "timestamp": timestamp,
                                    "step": "tool_result",
                                    "title": f"Tool Result ({status_str})",
                                    "content": result_text,
                                    "icon": icon,
                                    "style": "tool_result" if status_str == "success" else "error",
                                    "tool_use_id": tool_use_id,
                                })

            elif isinstance(content_raw, str):
                # If the raw string is a JSON array of toolResult blocks, parse them
                # as tool results rather than rendering as Agent text.
                stripped = content_raw.lstrip()
                if stripped.startswith("[") and "toolResult" in stripped[:80]:
                    try:
                        parsed_arr = json.loads(content_raw)
                    except (json.JSONDecodeError, TypeError):
                        parsed_arr = []
                    for block in parsed_arr if isinstance(parsed_arr, list) else []:
                        if not isinstance(block, dict) or "toolResult" not in block:
                            continue
                        tr = block["toolResult"]
                        tool_use_id = tr.get("toolUseId", "")
                        msg_key = f"tool_result:{tool_use_id}" if tool_use_id else f"tool_result:{json.dumps(tr)[:200]}"
                        if msg_key in seen_messages:
                            continue
                        seen_messages.add(msg_key)
                        text_parts = []
                        for c in tr.get("content", []):
                            if isinstance(c, dict) and "text" in c:
                                text_parts.append(c["text"])
                        result_text = "\n".join(text_parts) if text_parts else json.dumps(tr.get("content", ""), indent=2)
                        try:
                            result_text = json.dumps(json.loads(result_text), indent=2)
                        except (json.JSONDecodeError, TypeError):
                            pass
                        status_str = tr.get("status", "success")
                        icon = "✅" if status_str == "success" else "❌"
                        conversation.append({
                            "timestamp": timestamp,
                            "step": "tool_result",
                            "title": f"Tool Result ({status_str})",
                            "content": result_text,
                            "icon": icon,
                            "style": "tool_result" if status_str == "success" else "error",
                            "tool_use_id": tool_use_id,
                        })
                    continue

                # Skip JSON-shaped strings that are tool-result echoes, not real prose
                if stripped.startswith(("{", "[")):
                    continue

                msg_key = f"assistant:{content_raw[:200]}"
                if msg_key not in seen_messages:
                    seen_messages.add(msg_key)
                    conversation.append({
                        "timestamp": timestamp,
                        "step": "assistant",
                        "title": "Agent",
                        "content": content_raw,
                        "icon": "🤖",
                        "style": "assistant",
                    })

    return conversation


def _extract_message_text(content_raw):
    """Extract readable text from various message content formats."""
    if isinstance(content_raw, str):
        try:
            parsed = json.loads(content_raw)
            if isinstance(parsed, list):
                texts = []
                for item in parsed:
                    if isinstance(item, dict) and "text" in item:
                        texts.append(item["text"])
                return "\n".join(texts) if texts else content_raw
            return content_raw
        except (json.JSONDecodeError, TypeError):
            return content_raw
    elif isinstance(content_raw, dict):
        inner = content_raw.get("content", content_raw.get("message", ""))
        if isinstance(inner, str):
            try:
                parsed = json.loads(inner)
                if isinstance(parsed, list):
                    texts = []
                    for item in parsed:
                        if isinstance(item, dict) and "text" in item:
                            texts.append(item["text"])
                    return "\n".join(texts) if texts else inner
                return inner
            except (json.JSONDecodeError, TypeError):
                return inner
        return str(inner) if inner else ""
    elif isinstance(content_raw, list):
        texts = []
        for item in content_raw:
            if isinstance(item, dict) and "text" in item:
                texts.append(item["text"])
        return "\n".join(texts)
    return ""


def _parse_content_blocks(text):
    """Parse a JSON array of content blocks into structured items."""
    if not text:
        return []

    try:
        blocks = json.loads(text) if isinstance(text, str) else text
    except (json.JSONDecodeError, TypeError):
        return [{"type": "text", "text": text}]

    if not isinstance(blocks, list):
        return [{"type": "text", "text": str(blocks)}]

    result = []
    for block in blocks:
        if not isinstance(block, dict):
            result.append({"type": "text", "text": str(block)})
            continue

        if "text" in block:
            result.append({"type": "text", "text": block["text"]})
        elif "toolUse" in block:
            tool_use = block["toolUse"]
            name = tool_use.get("name", "unknown")
            display = name.split("___")[-1] if "___" in name else name
            result.append({
                "type": "tool_use",
                "name": display,
                "input": tool_use.get("input", {}),
                "toolUseId": tool_use.get("toolUseId", ""),
            })
        elif "toolResult" in block:
            tool_result = block["toolResult"]
            text_parts = []
            for c in tool_result.get("content", []):
                if isinstance(c, dict) and "text" in c:
                    text_parts.append(c["text"])
            result.append({
                "type": "tool_result",
                "status": tool_result.get("status", "success"),
                "text": "\n".join(text_parts),
                "toolUseId": tool_result.get("toolUseId", ""),
            })

    return result


def _parse_tool_result_text(text):
    """Parse tool result text which may be JSON content blocks."""
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return json.dumps(parsed, indent=2)
        elif isinstance(parsed, list):
            texts = []
            for item in parsed:
                if isinstance(item, dict):
                    if "text" in item:
                        texts.append(item["text"])
                    elif "toolResult" in item:
                        for c in item["toolResult"].get("content", []):
                            if "text" in c:
                                texts.append(c["text"])
            return "\n".join(texts) if texts else text
    except (json.JSONDecodeError, TypeError):
        pass
    return text


def _extract_invocations_from_spans(spans):
    """Group spans into per-invocation buckets by time-window of AgentCore.Runtime.Invoke (or invoke_agent).

    Returns a list of dicts:
        {
          "start_ns": int, "end_ns": int, "timestamp": str, "duration_ms": float,
          "model": str, "tools_available": [str], "input_tokens": int, "output_tokens": int,
          "gateway_init": [{"path": str, "host": str, "status": str, "duration_ms": float}],
          "rounds": [
              {
                "llm": {"model","input_tokens","output_tokens","ttft_ms","duration_ms"},
                "tool": {"name","status","input","duration_ms","gateway_status","gateway_duration_ms"} or None,
                "errors": [str]
              }
          ]
        }
    """
    if not spans:
        return []

    # Prefer AgentCore.Runtime.Invoke as the root. Strands invoke_agent is a CHILD
    # of Runtime.Invoke, so we only fall back to invoke_agent when there's no AgentCore root.
    runtime_roots = [s for s in spans if s.get("name") == "AgentCore.Runtime.Invoke"]
    if runtime_roots:
        roots = runtime_roots
    else:
        roots = [
            s for s in spans
            if s.get("name", "").startswith("invoke_agent")
            and s.get("attributes", {}).get("gen_ai.agent.tools")
        ]

    invocations = []
    for root in roots:
        start_ns = root.get("startTimeUnixNano", 0)
        end_ns = root.get("endTimeUnixNano", 0)
        children = [
            s for s in spans
            if s is not root and start_ns <= s.get("startTimeUnixNano", 0) <= end_ns
        ]
        children.sort(key=lambda s: s.get("startTimeUnixNano", 0))

        inv = {
            "start_ns": start_ns,
            "end_ns": end_ns,
            "timestamp": root.get("_timestamp", ""),
            "duration_ms": round(root.get("durationNano", 0) / 1_000_000, 1),
            "model": "",
            "tools_available": [],
            "input_tokens": 0,
            "output_tokens": 0,
            "gateway_init": [],
            "rounds": [],
        }

        # Find the invoke_agent span (carries model + tools list + total tokens)
        invoke_agent_span = None
        for s in children:
            if s.get("name", "").startswith("invoke_agent"):
                invoke_agent_span = s
                break
        if root.get("name", "").startswith("invoke_agent") and not invoke_agent_span:
            invoke_agent_span = root

        if invoke_agent_span:
            attrs = invoke_agent_span.get("attributes", {})
            inv["model"] = attrs.get("gen_ai.request.model", "")
            inv["input_tokens"] = attrs.get("gen_ai.usage.input_tokens", 0)
            inv["output_tokens"] = attrs.get("gen_ai.usage.output_tokens", 0)
            tools_raw = attrs.get("gen_ai.agent.tools", "[]")
            try:
                tools = json.loads(tools_raw) if isinstance(tools_raw, str) else tools_raw
            except Exception:
                tools = []
            inv["tools_available"] = [t.split("___")[-1] if "___" in t else t for t in (tools or [])]
            invoke_agent_start = invoke_agent_span.get("startTimeUnixNano", start_ns)
        else:
            invoke_agent_start = start_ns

        # Pre-invoke_agent gateway POSTs = MCP initialization (Agent ↔ Gateway: list tools)
        for s in children:
            if s.get("startTimeUnixNano", 0) >= invoke_agent_start:
                continue
            if s.get("kind") != "CLIENT":
                continue
            name = s.get("name", "")
            if not (name == "POST" or name.startswith("POST ") or name.startswith("GET")):
                continue
            url = s.get("attributes", {}).get("http.url", "") or s.get("attributes", {}).get("http.target", "")
            if not ("gateway" in url or "mcp" in url):
                continue
            from urllib.parse import urlparse
            parsed = urlparse(url) if url else None
            inv["gateway_init"].append({
                "host": parsed.hostname if parsed else "",
                "path": parsed.path if parsed else "",
                "status": str(s.get("attributes", {}).get("http.status_code", "")),
                "duration_ms": round(s.get("durationNano", 0) / 1_000_000, 1),
            })

        # Build rounds: each round = a chat span (LLM call), optionally followed by execute_tool + gateway POST
        chat_spans = [
            s for s in children
            if (s.get("name") == "chat" or s.get("name", "").startswith("chat "))
            and s.get("kind") == "INTERNAL"
            and s.get("startTimeUnixNano", 0) >= invoke_agent_start
        ]
        tool_spans = [
            s for s in children
            if "execute_tool" in s.get("name", "")
            and s.get("startTimeUnixNano", 0) >= invoke_agent_start
        ]

        for chat in chat_spans:
            attrs = chat.get("attributes", {})
            chat_end = chat.get("endTimeUnixNano", 0)
            # Find the next tool that starts after this chat ends, before the next chat
            next_chat_start = None
            for c2 in chat_spans:
                if c2.get("startTimeUnixNano", 0) > chat.get("startTimeUnixNano", 0):
                    next_chat_start = c2.get("startTimeUnixNano", 0)
                    break
            tool_for_round = None
            for t in tool_spans:
                t_start = t.get("startTimeUnixNano", 0)
                if t_start <= chat_end:
                    continue
                if next_chat_start is not None and t_start >= next_chat_start:
                    break
                tool_for_round = t
                break

            round_entry = {
                "llm": {
                    "model": attrs.get("gen_ai.request.model", inv["model"]),
                    "input_tokens": attrs.get("gen_ai.usage.input_tokens", 0),
                    "output_tokens": attrs.get("gen_ai.usage.output_tokens", 0),
                    "ttft_ms": attrs.get("gen_ai.server.time_to_first_token", ""),
                    "duration_ms": round(chat.get("durationNano", 0) / 1_000_000, 1),
                    "finish_reason": attrs.get("gen_ai.response.finish_reasons", ""),
                },
                "tool": None,
                "errors": [],
            }

            if tool_for_round:
                t_attrs = tool_for_round.get("attributes", {})
                tool_name = t_attrs.get("gen_ai.tool.name", "")
                display = tool_name.split("___")[-1] if "___" in tool_name else tool_name
                # Find the matching gateway POST inside the execute_tool's time window
                gateway = None
                for s in children:
                    if s.get("kind") != "CLIENT":
                        continue
                    n = s.get("name", "")
                    if not (n == "POST" or n.startswith("POST ")):
                        continue
                    if (tool_for_round.get("startTimeUnixNano", 0)
                            <= s.get("startTimeUnixNano", 0)
                            <= tool_for_round.get("endTimeUnixNano", 0)):
                        url = s.get("attributes", {}).get("http.url", "") or ""
                        if "gateway" in url or "mcp" in url:
                            from urllib.parse import urlparse
                            parsed = urlparse(url) if url else None
                            gateway = {
                                "host": parsed.hostname if parsed else "",
                                "path": parsed.path if parsed else "",
                                "status": str(s.get("attributes", {}).get("http.status_code", "")),
                                "duration_ms": round(s.get("durationNano", 0) / 1_000_000, 1),
                            }
                            break
                round_entry["tool"] = {
                    "name": display,
                    "status": t_attrs.get("gen_ai.tool.status", ""),
                    "tool_use_id": t_attrs.get("gen_ai.tool.call.id", ""),
                    "duration_ms": round(tool_for_round.get("durationNano", 0) / 1_000_000, 1),
                    "gateway": gateway,
                }
                # Capture exception events from execute_tool
                for evt in tool_for_round.get("events", []):
                    ea = evt.get("attributes", {})
                    if ea.get("exception.type"):
                        round_entry["errors"].append(
                            f"{ea.get('exception.type', '')}: {ea.get('exception.message', '')[:300]}"
                        )

            inv["rounds"].append(round_entry)

        invocations.append(inv)

    invocations.sort(key=lambda i: i["start_ns"])
    return invocations


def _extract_user_and_assistant_turns(runtime_conversation):
    """Pull out user messages and assistant final replies (skip tool_call/tool_result rows)."""
    turns = []
    for s in runtime_conversation or []:
        style = s.get("style", "")
        if style == "user":
            turns.append({"role": "user", "content": s.get("content", ""), "timestamp": s.get("timestamp", "")})
        elif style == "assistant":
            turns.append({"role": "assistant", "content": s.get("content", ""), "timestamp": s.get("timestamp", "")})
    return turns


def _strip_user_context_wrapper(text):
    """Strip the <user_context>...</user_context> envelope and 'Current customer message:' prefix the SDK adds."""
    if not text:
        return text
    cleaned = re.sub(r"<user_context>.*?</user_context>\s*", "", text, flags=re.DOTALL)
    m = re.search(r"Current customer message:\s*(.+)", cleaned, re.DOTALL)
    if m:
        cleaned = m.group(1)
    return cleaned.strip()


def build_actor_flow_narrative(spans, runtime_conversation):
    """
    Render the conversation as an explicit actor-flow timeline:
        1. User → Agent
        2. Agent → Gateway (list tools)            [if MCP init present]
        3. Agent → LLM (which tool?)
        4. LLM → Agent (use tool X)                [if LLM picked a tool]
        5. Agent → Gateway (execute tool)          [if tool used]
        6. Gateway → Agent (tool result)           [if tool used]
        7. Agent → LLM (here's the result)         [next round]
        8. LLM → Agent (final answer)
        9. Agent → User
    Multiple LLM↔tool rounds are rendered as repeated 3-6 cycles.
    """
    invocations = _extract_invocations_from_spans(spans)
    turns = _extract_user_and_assistant_turns(runtime_conversation)

    # Pair user messages and assistant replies to invocations in order.
    user_turns = [t for t in turns if t["role"] == "user"]
    assistant_turns = [t for t in turns if t["role"] == "assistant"]

    steps = []
    for idx, inv in enumerate(invocations):
        ts = inv["timestamp"]
        # 1. User → Agent
        user_text = _strip_user_context_wrapper(user_turns[idx]["content"]) if idx < len(user_turns) else ""
        if user_text:
            steps.append({
                "timestamp": user_turns[idx]["timestamp"] if idx < len(user_turns) else ts,
                "step": "user_to_agent",
                "title": "1️⃣ User → Agent",
                "content": user_text,
                "icon": "👤",
                "style": "user",
            })

        # 2. Agent → Gateway (MCP init / list tools) — only on first invocation usually,
        #    but emit per-invocation if the trace shows it.
        if inv["gateway_init"]:
            count = len(inv["gateway_init"])
            total_ms = round(sum(g["duration_ms"] for g in inv["gateway_init"]), 1)
            tools_list = inv["tools_available"]
            content = (
                f"Connected to MCP gateway and loaded {len(tools_list)} available tools.\n"
                f"Tools: {', '.join(tools_list[:8])}"
                + (f" (+{len(tools_list)-8} more)" if len(tools_list) > 8 else "")
                + f"\n_{count} init requests, {total_ms}ms total_"
            )
            steps.append({
                "timestamp": ts,
                "step": "agent_to_gateway_init",
                "title": "2️⃣ Agent → Gateway (list tools)",
                "content": content,
                "icon": "🌐",
                "style": "gateway",
            })

        # 3-8. Iterate rounds: Agent→LLM, LLM→Agent, [Agent→Gateway, Gateway→Agent], next round...
        rounds = inv["rounds"]
        for r_idx, rnd in enumerate(rounds):
            llm = rnd["llm"]
            round_label = f" (round {r_idx + 1}/{len(rounds)})" if len(rounds) > 1 else ""

            # 3. Agent → LLM
            llm_in_content = (
                f"Sent to **{llm['model'] or inv['model']}**\n"
                f"Input: {llm['input_tokens']} tokens"
            )
            steps.append({
                "timestamp": ts,
                "step": "agent_to_llm",
                "title": f"3️⃣ Agent → LLM{round_label}",
                "content": llm_in_content,
                "icon": "🧠",
                "style": "llm",
            })

            # 4. LLM → Agent
            tool = rnd["tool"]
            ttft_str = f", TTFT {llm['ttft_ms']}ms" if llm["ttft_ms"] else ""
            if tool:
                llm_out_content = (
                    f"LLM decided to call tool: **{tool['name']}**\n"
                    f"Output: {llm['output_tokens']} tokens in {llm['duration_ms']}ms{ttft_str}"
                )
            else:
                llm_out_content = (
                    f"LLM produced final answer (no further tool calls).\n"
                    f"Output: {llm['output_tokens']} tokens in {llm['duration_ms']}ms{ttft_str}"
                )
                if llm.get("finish_reason"):
                    llm_out_content += f"\nFinish reason: {llm['finish_reason']}"
            steps.append({
                "timestamp": ts,
                "step": "llm_to_agent",
                "title": f"4️⃣ LLM → Agent{round_label}",
                "content": llm_out_content,
                "icon": "💬",
                "style": "llm",
            })

            if tool:
                # 5. Agent → Gateway (execute tool)
                gw = tool.get("gateway") or {}
                tool_input = _find_tool_input_in_runtime(runtime_conversation, tool.get("tool_use_id"), tool["name"])
                input_block = (
                    f"```json\n{tool_input}\n```\n" if tool_input else ""
                )
                steps.append({
                    "timestamp": ts,
                    "step": "agent_to_gateway_exec",
                    "title": f"5️⃣ Agent → Gateway (execute {tool['name']})",
                    "content": (
                        f"Executing tool **{tool['name']}** via MCP gateway\n"
                        + input_block
                        + (f"_Gateway: {gw.get('host','')} | POST {gw.get('path','')}_" if gw else "")
                    ),
                    "icon": "🔧",
                    "style": "gateway",
                })

                # 6. Gateway → Agent (tool result)
                tool_result = _find_tool_result_in_runtime(runtime_conversation, tool.get("tool_use_id"))
                status_icon = "✅" if tool["status"] == "success" else "❌"
                gw_status = gw.get("status", "")
                gw_dur = gw.get("duration_ms", "")
                meta_line = f"_Status: {status_icon} {tool['status']} | Tool span: {tool['duration_ms']}ms"
                if gw_status:
                    meta_line += f" | HTTP {gw_status}"
                if gw_dur:
                    meta_line += f" | Gateway: {gw_dur}ms"
                meta_line += "_"
                steps.append({
                    "timestamp": ts,
                    "step": "gateway_to_agent",
                    "title": f"6️⃣ Gateway → Agent (result)",
                    "content": (
                        (f"```json\n{tool_result}\n```\n" if tool_result else "")
                        + meta_line
                    ),
                    "icon": status_icon,
                    "style": "tool_result" if tool["status"] == "success" else "error",
                })

                if rnd.get("errors"):
                    for err in rnd["errors"]:
                        steps.append({
                            "timestamp": ts,
                            "step": "tool_error",
                            "title": "⚠️ Tool Error",
                            "content": err,
                            "icon": "❌",
                            "style": "error",
                        })

        # 9. Agent → User
        agent_text = assistant_turns[idx]["content"] if idx < len(assistant_turns) else ""
        if agent_text:
            steps.append({
                "timestamp": assistant_turns[idx]["timestamp"] if idx < len(assistant_turns) else ts,
                "step": "agent_to_user",
                "title": "9️⃣ Agent → User",
                "content": agent_text,
                "icon": "🤖",
                "style": "assistant",
            })

        # Per-invocation summary
        steps.append({
            "timestamp": ts,
            "step": "summary",
            "title": f"⏱️ Invocation {idx + 1}/{len(invocations)}: {inv['duration_ms']}ms",
            "content": (
                f"Model: {inv['model'] or '?'} | "
                f"Tokens: {inv['input_tokens']} in → {inv['output_tokens']} out | "
                f"Rounds: {len(inv['rounds'])} | "
                f"Tools used: {sum(1 for r in inv['rounds'] if r['tool'])}"
            ),
            "icon": "📊",
            "style": "summary",
        })

    return steps


def _find_tool_input_in_runtime(runtime_conversation, tool_use_id, tool_name):
    """Look up the JSON input for a tool from the runtime-log conversation rows."""
    if not runtime_conversation:
        return ""
    # Prefer match by tool_use_id (set on tool_call rows after parser changes)
    if tool_use_id:
        for s in runtime_conversation:
            if s.get("style") == "tool_call" and s.get("tool_use_id") == tool_use_id:
                return _extract_json_block(s.get("content", ""))
    # Fallback: match by tool name in the title
    for s in runtime_conversation:
        if s.get("style") == "tool_call" and tool_name and tool_name in s.get("title", ""):
            return _extract_json_block(s.get("content", ""))
    return ""


def _find_tool_result_in_runtime(runtime_conversation, tool_use_id):
    """Look up the result content (truncated) for a tool from runtime-log conversation rows."""
    if not runtime_conversation or not tool_use_id:
        return ""
    for s in runtime_conversation:
        if s.get("style") in ("tool_result", "error") and s.get("tool_use_id") == tool_use_id:
            content = s.get("content", "")
            if len(content) > 1500:
                content = content[:1500] + "\n... (truncated)"
            return content
    return ""


def _extract_json_block(markdown_text):
    """Pull the JSON inside the first ```json ... ``` fence."""
    m = re.search(r"```json\s*\n(.*?)\n```", markdown_text, re.DOTALL)
    return m.group(1).strip() if m else ""


def build_clean_narrative(spans, event_messages):
    """
    Build a clean, step-by-step narrative combining spans and events.
    Includes: user messages, agent↔LLM exchanges, agent↔gateway calls, tool results.
    """
    steps = []

    # Parse spans into invocations with detailed sub-steps
    invocations = []
    current_invocation = None

    for span in spans:
        name = span.get("name", "")
        kind = span.get("kind", "")
        attrs = span.get("attributes", {})
        duration_ns = span.get("durationNano", 0)
        duration_ms = round(duration_ns / 1_000_000, 1)
        timestamp = span.get("_timestamp", "")
        events = span.get("events", [])

        # Detect invocation start: AgentCore native or Strands SDK
        is_invocation_start = (
            name == "AgentCore.Runtime.Invoke"
            or (name.startswith("invoke_agent") and attrs.get("gen_ai.agent.tools"))
        )

        if is_invocation_start:
            if current_invocation:
                invocations.append(current_invocation)
            current_invocation = {
                "timestamp": timestamp,
                "total_duration_ms": duration_ms,
                "sub_steps": [],
            }
            # For Strands SDK, the invoke_agent span IS the invocation AND contains
            # model/token/tool info (unlike AgentCore where it's a child span)
            if "invoke_agent" in name and name != "AgentCore.Runtime.Invoke":
                model = attrs.get("gen_ai.request.model", "unknown")
                input_tokens = attrs.get("gen_ai.usage.input_tokens", 0)
                output_tokens = attrs.get("gen_ai.usage.output_tokens", 0)
                tools_raw = attrs.get("gen_ai.agent.tools", "[]")
                try:
                    tools = json.loads(tools_raw) if isinstance(tools_raw, str) else tools_raw
                except Exception:
                    tools = []
                tool_names = [t.split("___")[-1] if "___" in t else t for t in tools]

                current_invocation["model"] = model
                current_invocation["tools"] = tool_names
                current_invocation["input_tokens"] = input_tokens
                current_invocation["output_tokens"] = output_tokens

                llm_content = (
                    f"Sent request to **{model}**\n"
                    f"System prompt + conversation history: {input_tokens} tokens\n"
                    f"Tools provided: {', '.join(tool_names)}\n"
                    f"LLM generated: {output_tokens} tokens\n"
                    f"Total duration: {duration_ms}ms"
                )
                current_invocation["sub_steps"].append({
                    "icon": "🧠",
                    "title": "Agent → LLM",
                    "style": "llm",
                    "content": llm_content,
                })

        elif current_invocation is not None:
            # Agent → LLM: invoke_agent (shows tools sent to LLM)
            if "invoke_agent" in name:
                model = attrs.get("gen_ai.request.model", "unknown")
                input_tokens = attrs.get("gen_ai.usage.input_tokens", 0)
                output_tokens = attrs.get("gen_ai.usage.output_tokens", 0)
                tools_raw = attrs.get("gen_ai.agent.tools", "[]")
                try:
                    tools = json.loads(tools_raw) if isinstance(tools_raw, str) else tools_raw
                except Exception:
                    tools = []
                tool_names = [t.split("___")[-1] if "___" in t else t for t in tools]

                current_invocation["model"] = model
                current_invocation["tools"] = tool_names
                current_invocation["input_tokens"] = input_tokens
                current_invocation["output_tokens"] = output_tokens

                # Build content with actual LLM messages if available
                llm_content = (
                    f"Sent request to **{model}**\n"
                    f"System prompt + conversation history: {input_tokens} tokens\n"
                    f"Tools provided: {', '.join(tool_names)}\n"
                    f"LLM generated: {output_tokens} tokens\n"
                    f"Total duration: {duration_ms}ms"
                )

                current_invocation["sub_steps"].append({
                    "icon": "🧠",
                    "title": "Agent → LLM",
                    "style": "llm",
                    "content": llm_content,
                })

            # LLM thinking (chat span with TTFT = the real one)
            elif (name == "chat" or name.startswith("chat ")) and attrs.get("gen_ai.server.time_to_first_token"):
                model = attrs.get("gen_ai.request.model", "")
                input_tokens = attrs.get("gen_ai.usage.input_tokens", 0)
                output_tokens = attrs.get("gen_ai.usage.output_tokens", 0)
                ttft = attrs.get("gen_ai.server.time_to_first_token", "")
                request_duration = attrs.get("gen_ai.server.request.duration", "")

                current_invocation["sub_steps"].append({
                    "icon": "💬",
                    "title": "LLM Thinking",
                    "style": "llm",
                    "content": (
                        f"Model: `{model}`\n"
                        f"Input: {input_tokens} tokens → Output: {output_tokens} tokens\n"
                        f"Time to first token: {ttft}ms\n"
                        f"Response time: {request_duration}ms | Total: {duration_ms}ms"
                    ),
                })

            # Tool execution
            elif "execute_tool" in name:
                tool_name = attrs.get("gen_ai.tool.name", "")
                tool_status = attrs.get("gen_ai.tool.status", "")
                display_name = tool_name.split("___")[-1] if "___" in tool_name else tool_name
                icon = "✅" if tool_status == "success" else "❌"

                current_invocation["sub_steps"].append({
                    "icon": "🔧",
                    "title": f"LLM → Agent: Use tool '{display_name}'",
                    "style": "tool_call",
                    "content": f"Agent executed tool: **{display_name}**\nStatus: {icon} {tool_status} | Duration: {duration_ms}ms",
                })

                # Errors
                for evt in events:
                    evt_attrs = evt.get("attributes", {})
                    if evt_attrs.get("exception.type"):
                        current_invocation["sub_steps"].append({
                            "icon": "❌",
                            "title": "Error",
                            "style": "error",
                            "content": f"**{evt_attrs['exception.type']}**\n{evt_attrs.get('exception.message', '')[:300]}",
                        })

            # Gateway calls (agent calling MCP gateway)
            elif kind == "CLIENT" and ("POST" in name or "GET" in name) and "invocations" not in name:
                url = attrs.get("http.url", attrs.get("http.target", ""))
                status_code = attrs.get("http.status_code", attrs.get("http.response.status_code", ""))

                # Only include gateway/MCP calls, skip internal
                if "gateway" in url or "mcp" in url or "bedrock-agentcore" in url:
                    # Determine if this is a tool execution call (longer, after execute_tool)
                    # or an init/handshake call (short, at start)
                    # Init calls are typically < 400ms and happen before any execute_tool
                    has_tool_execution = any(
                        s.get("title", "").startswith("LLM → Agent")
                        for s in current_invocation["sub_steps"]
                    )

                    if not has_tool_execution and duration_ms < 400:
                        # This is MCP initialization (listing tools, handshake)
                        # Group them - only add one summary entry
                        if not any(s.get("title") == "Gateway: MCP Initialization" for s in current_invocation["sub_steps"]):
                            current_invocation["sub_steps"].append({
                                "icon": "🌐",
                                "title": "Gateway: MCP Initialization",
                                "style": "gateway",
                                "content": "Connecting to MCP gateway and loading available tools...",
                                "_is_init_placeholder": True,
                            })
                        # Update the placeholder with count
                        for s in current_invocation["sub_steps"]:
                            if s.get("_is_init_placeholder"):
                                count = s.get("_init_count", 0) + 1
                                s["_init_count"] = count
                                total_dur = s.get("_init_total_ms", 0) + duration_ms
                                s["_init_total_ms"] = total_dur
                                s["content"] = f"Connected to MCP gateway, loaded tools ({count} requests, {total_dur:.0f}ms total)"
                    else:
                        # This is an actual tool execution gateway call
                        from urllib.parse import urlparse
                        parsed = urlparse(url) if url else None
                        host = parsed.hostname if parsed else ""
                        path = parsed.path if parsed else ""
                        status_icon = "✅" if str(status_code).startswith("2") else "❌"
                        current_invocation["sub_steps"].append({
                            "icon": "🌐",
                            "title": "Agent → Gateway (tool execution)",
                            "style": "gateway",
                            "content": f"**POST** {path}\nGateway: {host}\nStatus: {status_icon} {status_code} | Duration: {duration_ms}ms",
                        })

            # Bedrock API calls (ListEvents, CreateEvent, etc.)
            elif kind == "CLIENT" and "Bedrock" in name:
                remote_op = attrs.get("aws.remote.operation", attrs.get("rpc.method", ""))
                status_code = attrs.get("http.status_code", "")
                # Only show non-trivial ones
                if remote_op not in ("ListEvents", "CreateEvent"):
                    status_icon = "✅" if str(status_code).startswith("2") else "❌"
                    current_invocation["sub_steps"].append({
                        "icon": "☁️",
                        "title": f"Agent → AWS: {remote_op}",
                        "style": "aws_api",
                        "content": f"**{name}**\nStatus: {status_icon} {status_code} | Duration: {duration_ms}ms",
                    })

    if current_invocation:
        invocations.append(current_invocation)

    # Now build the final narrative: interleave event messages with span sub-steps
    if event_messages and invocations:
        inv_idx = 0
        i = 0
        while i < len(event_messages):
            msg = event_messages[i]

            if msg["role"] == "user" and msg.get("type") == "user_message":
                # User message
                steps.append({
                    "timestamp": msg["timestamp"],
                    "step": "user_message",
                    "title": "User",
                    "content": msg["content"],
                    "icon": "👤",
                    "style": "user",
                })
                i += 1

                # Add span sub-steps for this invocation (agent↔LLM, gateway calls)
                if inv_idx < len(invocations):
                    inv = invocations[inv_idx]
                    for sub in inv["sub_steps"]:
                        steps.append({
                            "timestamp": inv["timestamp"],
                            "step": sub.get("title", ""),
                            "title": sub["title"],
                            "content": sub["content"],
                            "icon": sub["icon"],
                            "style": sub["style"],
                        })

                # Continue with tool calls and tool results until we hit agent response or next user msg
                while i < len(event_messages):
                    msg = event_messages[i]
                    if msg["role"] == "user" and msg.get("type") == "user_message":
                        # Next user message — break out to handle it in the outer loop
                        # First close the current invocation with a summary
                        if inv_idx < len(invocations):
                            inv = invocations[inv_idx]
                            steps.append({
                                "timestamp": inv["timestamp"],
                                "step": "summary",
                                "title": f"⏱️ Total: {inv['total_duration_ms']}ms",
                                "content": f"Model: {inv.get('model','unknown')} | Tokens: {inv.get('input_tokens', '?')} in → {inv.get('output_tokens', '?')} out",
                                "icon": "📊",
                                "style": "summary",
                            })
                            inv_idx += 1
                        break
                    elif msg["role"] == "assistant" and msg.get("type") == "assistant_message":
                        # Agent response
                        steps.append({
                            "timestamp": msg["timestamp"],
                            "step": "agent_response",
                            "title": "Agent → User",
                            "content": msg["content"],
                            "icon": "🤖",
                            "style": "assistant",
                        })
                        i += 1

                        # Add summary
                        if inv_idx < len(invocations):
                            inv = invocations[inv_idx]
                            steps.append({
                                "timestamp": inv["timestamp"],
                                "step": "summary",
                                "title": f"⏱️ Total: {inv['total_duration_ms']}ms",
                                "content": f"Model: {inv.get('model','unknown')} | Tokens: {inv.get('input_tokens', '?')} in → {inv.get('output_tokens', '?')} out",
                                "icon": "📊",
                                "style": "summary",
                            })
                            inv_idx += 1
                        break
                    elif msg["role"] == "tool_call":
                        steps.append({
                            "timestamp": msg["timestamp"],
                            "step": "tool_call",
                            "title": f"LLM decided: call '{msg.get('tool_name', '')}'",
                            "content": msg["content"],
                            "icon": "🔧",
                            "style": "tool_call",
                        })
                        i += 1
                    elif msg["role"] == "tool_result":
                        steps.append({
                            "timestamp": msg["timestamp"],
                            "step": "tool_result",
                            "title": f"Tool response ({msg.get('status', '')})",
                            "content": msg["content"],
                            "icon": "📥" if msg.get("status") != "error" else "❌",
                            "style": "tool_result" if msg.get("status") != "error" else "error",
                        })
                        i += 1
                    else:
                        i += 1
                        break
            else:
                # Non-user-message at top level (orphan tool calls, assistant msgs)
                if msg["role"] == "tool_call":
                    steps.append({
                        "timestamp": msg["timestamp"],
                        "step": "tool_call",
                        "title": f"LLM decided: call '{msg.get('tool_name', '')}'",
                        "content": msg["content"],
                        "icon": "🔧",
                        "style": "tool_call",
                    })
                elif msg["role"] == "tool_result":
                    steps.append({
                        "timestamp": msg["timestamp"],
                        "step": "tool_result",
                        "title": f"Tool response ({msg.get('status', '')})",
                        "content": msg["content"],
                        "icon": "📥" if msg.get("status") != "error" else "❌",
                        "style": "tool_result" if msg.get("status") != "error" else "error",
                    })
                elif msg["role"] == "assistant" and msg.get("type") == "assistant_message":
                    steps.append({
                        "timestamp": msg["timestamp"],
                        "step": "agent_response",
                        "title": "Agent → User",
                        "content": msg["content"],
                        "icon": "🤖",
                        "style": "assistant",
                    })
                    if inv_idx < len(invocations):
                        inv_idx += 1
                i += 1

        return steps

    if invocations:
        for inv in invocations:
            for sub in inv["sub_steps"]:
                steps.append({
                    "timestamp": inv["timestamp"],
                    "step": sub.get("title", ""),
                    "title": sub["title"],
                    "content": sub["content"],
                    "icon": sub["icon"],
                    "style": sub["style"],
                })
        return steps

    return build_conversation_from_spans(spans)


def load_conversation_from_events(region, memory_id, actor_id, session_id):
    """Load full agent<->LLM conversation from Bedrock AgentCore Events API."""
    client = boto3.client("bedrock-agentcore", region_name=region)

    events = []
    try:
        paginator = client.get_paginator("list_events")
        for page in paginator.paginate(
            memoryId=memory_id,
            actorId=actor_id,
            sessionId=session_id,
        ):
            events.extend(page.get("events", []))
    except Exception as e:
        try:
            result = client.list_events(
                memoryId=memory_id,
                actorId=actor_id,
                sessionId=session_id,
            )
            events = result.get("events", [])
        except Exception:
            return [{"timestamp": "", "role": "system", "content": f"Could not load events: {str(e)}"}]

    # Sort events by timestamp (oldest first)
    events.sort(key=lambda e: e.get("eventTimestamp", ""))

    # Parse full agent<->LLM conversation from events
    conversation = []
    for event in events:
        timestamp = str(event.get("eventTimestamp", ""))
        payload_list = event.get("payload", [])

        for payload in payload_list:
            if "conversational" not in payload:
                continue

            text_raw = payload["conversational"]["content"]["text"]
            try:
                inner = json.loads(text_raw)
            except (json.JSONDecodeError, KeyError):
                continue

            msg = inner.get("message", {})
            role = msg.get("role", "unknown")
            content_parts = msg.get("content", [])

            for part in content_parts:
                if "text" in part:
                    text = part["text"]
                    # Determine if this is a user message to the agent
                    import re
                    customer_match = re.search(
                        r"Current customer message:\s*(.+)",
                        text, re.DOTALL
                    )
                    if customer_match and role == "user":
                        # This is the actual user input
                        user_msg = customer_match.group(1).strip()
                        conversation.append({
                            "timestamp": timestamp,
                            "role": "user",
                            "content": user_msg,
                            "type": "user_message",
                        })
                    elif role == "user" and "<user_context>" in text:
                        # User context without customer message (shouldn't happen but handle)
                        cleaned = re.sub(r"<user_context>.*?</user_context>\s*", "", text, flags=re.DOTALL).strip()
                        if cleaned:
                            conversation.append({
                                "timestamp": timestamp,
                                "role": "user",
                                "content": cleaned,
                                "type": "user_message",
                            })
                    elif role == "assistant":
                        # This is the agent's text response to the user
                        conversation.append({
                            "timestamp": timestamp,
                            "role": "assistant",
                            "content": text,
                            "type": "assistant_message",
                        })
                    elif role == "user":
                        # Plain user message without context wrapper
                        conversation.append({
                            "timestamp": timestamp,
                            "role": "user",
                            "content": text,
                            "type": "user_message",
                        })

                elif "toolUse" in part:
                    tool = part["toolUse"]
                    tool_name = tool.get("name", "unknown")
                    display_name = tool_name.split("___")[-1] if "___" in tool_name else tool_name
                    tool_input = json.dumps(tool.get("input", {}), indent=2)

                    conversation.append({
                        "timestamp": timestamp,
                        "role": "tool_call",
                        "content": f"**{display_name}**\n```json\n{tool_input}\n```",
                        "type": "tool_use",
                        "tool_name": display_name,
                    })

                elif "toolResult" in part:
                    result = part["toolResult"]
                    status = result.get("status", "")
                    result_content = result.get("content", [])
                    result_text = ""
                    for rc in result_content:
                        if "text" in rc:
                            result_text = rc["text"][:1000]
                            break

                    # Try to pretty-print JSON tool results
                    try:
                        parsed = json.loads(result_text)
                        result_text = json.dumps(parsed, indent=2)[:1000]
                    except (json.JSONDecodeError, TypeError):
                        pass

                    conversation.append({
                        "timestamp": timestamp,
                        "role": "tool_result",
                        "content": result_text,
                        "type": "tool_result",
                        "status": status,
                    })

    return conversation


def load_llm_interactions_from_spans(region, session_id, start_time, end_time):
    """Load LLM calls, system prompts, and gateway interactions from aws/spans."""
    client = boto3.client("logs", region_name=region)

    query = f"""
        fields @timestamp, @message
        | filter @message like /"{session_id}"/
        | sort @timestamp asc
        | limit 1000
    """.strip()

    response = client.start_query(
        logGroupNames=[LOG_GROUPS["spans"]],
        startTime=start_time,
        endTime=end_time,
        queryString=query,
    )
    query_id = response["queryId"]

    status = "Running"
    results = []
    while status in ("Running", "Scheduled"):
        time.sleep(1)
        result = client.get_query_results(queryId=query_id)
        status = result.get("status", "Unknown")
        results = result.get("results", [])

    messages = []
    for row in results:
        msg_text = ""
        timestamp = ""
        for field in row:
            if field["field"] == "@message":
                msg_text = field["value"]
            elif field["field"] == "@timestamp":
                timestamp = field["value"]
        if not msg_text:
            continue
        try:
            span = json.loads(msg_text)
        except json.JSONDecodeError:
            continue

        name = span.get("name", "")
        kind = span.get("kind", "")
        attrs = span.get("attributes", {})
        duration_ns = span.get("durationNano", 0)
        duration_ms = round(duration_ns / 1_000_000, 1)
        events = span.get("events", [])

        # AgentCore Runtime Invoke — incoming request
        if name == "AgentCore.Runtime.Invoke":
            messages.append({
                "timestamp": timestamp,
                "role": "system",
                "content": f"⬇️ **Incoming Request** to agent runtime\nTotal processing time: {duration_ms}ms",
                "type": "runtime_invoke",
            })

        # invoke_agent — agent orchestration with available tools
        elif "invoke_agent" in name:
            model = attrs.get("gen_ai.request.model", "unknown")
            input_tokens = attrs.get("gen_ai.usage.input_tokens", 0)
            output_tokens = attrs.get("gen_ai.usage.output_tokens", 0)
            tools_raw = attrs.get("gen_ai.agent.tools", "[]")
            try:
                tools = json.loads(tools_raw) if isinstance(tools_raw, str) else tools_raw
            except Exception:
                tools = []
            tool_names = [t.split("___")[-1] if "___" in t else t for t in tools]

            messages.append({
                "timestamp": timestamp,
                "role": "llm",
                "content": (
                    f"🧠 **Agent → LLM** ({model})\n"
                    f"Available tools sent to LLM: {', '.join(tool_names)}\n"
                    f"Tokens: {input_tokens} input → {output_tokens} output\n"
                    f"Duration: {duration_ms}ms"
                ),
                "type": "agent_to_llm",
            })

        # chat — individual LLM call (agent sending messages to model)
        elif name == "chat" or name.startswith("chat "):
            model = attrs.get("gen_ai.request.model", "")
            input_tokens = attrs.get("gen_ai.usage.input_tokens", 0)
            output_tokens = attrs.get("gen_ai.usage.output_tokens", 0)
            ttft = attrs.get("gen_ai.server.time_to_first_token", "")
            request_duration = attrs.get("gen_ai.server.request.duration", "")

            messages.append({
                "timestamp": timestamp,
                "role": "llm",
                "content": (
                    f"💬 **LLM Call** → `{model}`\n"
                    f"Messages sent: {input_tokens} tokens\n"
                    f"LLM response: {output_tokens} tokens\n"
                    f"TTFT: {ttft}ms | Request duration: {request_duration}ms | Total: {duration_ms}ms"
                ),
                "type": "llm_call",
            })

        # execute_event_loop_cycle — agent reasoning cycle
        elif name == "execute_event_loop_cycle":
            messages.append({
                "timestamp": timestamp,
                "role": "system",
                "content": f"🔄 **Agent Event Loop Cycle** ({duration_ms}ms)",
                "type": "event_loop",
            })

        # Gateway/HTTP calls (tool execution via gateway)
        elif kind == "CLIENT" and ("POST" in name or "GET" in name) and "invocations" not in name:
            url = attrs.get("http.url", attrs.get("http.target", name))
            status_code = attrs.get("http.status_code", attrs.get("http.response.status_code", ""))
            remote_service = attrs.get("aws.remote.service", "")
            remote_op = attrs.get("aws.remote.operation", "")

            if remote_service and remote_op:
                desc = f"{remote_service}.{remote_op}"
            else:
                desc = url[:100] if url else name

            status_icon = "✅" if str(status_code).startswith("2") else "❌"
            messages.append({
                "timestamp": timestamp,
                "role": "gateway",
                "content": (
                    f"🌐 **Gateway Call**: {desc}\n"
                    f"Status: {status_icon} {status_code} | Duration: {duration_ms}ms"
                ),
                "type": "gateway_call",
            })

        # Bedrock Runtime calls (CountTokens, Converse, etc.)
        elif kind == "CLIENT" and "Bedrock" in name:
            remote_op = attrs.get("aws.remote.operation", attrs.get("rpc.method", ""))
            status_code = attrs.get("http.status_code", "")
            status_icon = "✅" if str(status_code).startswith("2") else "❌"

            messages.append({
                "timestamp": timestamp,
                "role": "llm",
                "content": (
                    f"☁️ **AWS API**: {name}\n"
                    f"Operation: {remote_op} | Status: {status_icon} {status_code} | Duration: {duration_ms}ms"
                ),
                "type": "aws_api_call",
            })

            # Add exceptions
            for evt in events:
                evt_attrs = evt.get("attributes", {})
                if evt_attrs.get("exception.type"):
                    messages.append({
                        "timestamp": timestamp,
                        "role": "error",
                        "content": (
                            f"❌ **{evt_attrs.get('exception.type', '')}**\n"
                            f"{evt_attrs.get('exception.message', '')[:400]}"
                        ),
                        "type": "error",
                    })

    return messages


def load_conversation_from_spans(region, session_id, start_time, end_time):
    """Fallback: reconstruct conversation timeline from aws/spans."""
    client = boto3.client("logs", region_name=region)

    query = f"""
        fields @timestamp, @message
        | filter @message like /"{session_id}"/
        | sort @timestamp asc
        | limit 1000
    """.strip()

    response = client.start_query(
        logGroupNames=[LOG_GROUPS["spans"]],
        startTime=start_time,
        endTime=end_time,
        queryString=query,
    )
    query_id = response["queryId"]

    status = "Running"
    results = []
    while status in ("Running", "Scheduled"):
        time.sleep(1)
        result = client.get_query_results(queryId=query_id)
        status = result.get("status", "Unknown")
        results = result.get("results", [])

    spans = []
    for row in results:
        message = ""
        timestamp = ""
        for field in row:
            if field["field"] == "@message":
                message = field["value"]
            elif field["field"] == "@timestamp":
                timestamp = field["value"]
        if message:
            try:
                span_data = json.loads(message)
                span_data["_timestamp"] = timestamp
                spans.append(span_data)
            except json.JSONDecodeError:
                continue

    spans.sort(key=lambda s: s.get("startTimeUnixNano", 0))
    return build_conversation_from_spans(spans)


def build_conversation_from_spans(spans):
    """Build a chat-like conversation timeline from OTEL spans."""
    conversation = []
    invocation_num = 0

    for span in spans:
        name = span.get("name", "")
        kind = span.get("kind", "")
        attrs = span.get("attributes", {})
        events = span.get("events", [])
        duration_ns = span.get("durationNano", 0)
        duration_ms = round(duration_ns / 1_000_000, 1)
        timestamp = span.get("_timestamp", "")
        status_code = span.get("status", {}).get("code", "")

        # 1. Runtime Invoke — marks a new user turn
        if name == "AgentCore.Runtime.Invoke":
            invocation_num += 1
            conversation.append({
                "timestamp": timestamp,
                "role": "user",
                "content": f"User request #{invocation_num}",
                "type": "request",
                "duration": duration_ms,
            })

        # 2. invoke_agent — full agent cycle with model & token info
        elif "invoke_agent" in name:
            model = attrs.get("gen_ai.request.model", "unknown")
            input_tokens = attrs.get("gen_ai.usage.input_tokens", 0)
            output_tokens = attrs.get("gen_ai.usage.output_tokens", 0)
            tools_raw = attrs.get("gen_ai.agent.tools", "[]")
            try:
                tools = json.loads(tools_raw) if isinstance(tools_raw, str) else tools_raw
            except Exception:
                tools = []
            # Clean tool names (remove gateway prefix)
            tool_names = [t.split("___")[-1] if "___" in t else t for t in tools]

            conversation.append({
                "timestamp": timestamp,
                "role": "assistant",
                "content": (
                    f"🧠 Agent invoked\n"
                    f"Model: {model}\n"
                    f"Tools available: {', '.join(tool_names[:6])}\n"
                    f"Tokens: {input_tokens} input → {output_tokens} output\n"
                    f"Duration: {duration_ms}ms"
                ),
                "type": "agent",
                "duration": duration_ms,
            })

        # 3. chat — LLM thinking step
        elif name == "chat" or name.startswith("chat "):
            model = attrs.get("gen_ai.request.model", "")
            input_tokens = attrs.get("gen_ai.usage.input_tokens", 0)
            output_tokens = attrs.get("gen_ai.usage.output_tokens", 0)
            ttft = attrs.get("gen_ai.server.time_to_first_token", "")

            conversation.append({
                "timestamp": timestamp,
                "role": "assistant",
                "content": (
                    f"🧠 Thinking... ({model})\n"
                    f"Generated {output_tokens} tokens in {duration_ms}ms"
                    + (f" (TTFT: {ttft}ms)" if ttft else "")
                ),
                "type": "thinking",
                "duration": duration_ms,
            })

        # 4. execute_tool — tool call
        elif "execute_tool" in name:
            tool_name = attrs.get("gen_ai.tool.name", name.replace("execute_tool ", ""))
            tool_status = attrs.get("gen_ai.tool.status", "unknown")
            tool_desc = attrs.get("gen_ai.tool.description", "")[:120]

            icon = "✅" if tool_status == "success" else "❌"
            content = f"{icon} Called tool: **{tool_name}**\nDuration: {duration_ms}ms"
            if tool_desc:
                content += f"\n_{tool_desc}_"

            conversation.append({
                "timestamp": timestamp,
                "role": "tool",
                "content": content,
                "type": "tool_call",
                "duration": duration_ms,
            })

            # Add error details if present
            for evt in events:
                evt_attrs = evt.get("attributes", {})
                if evt_attrs.get("exception.type"):
                    conversation.append({
                        "timestamp": timestamp,
                        "role": "error",
                        "content": (
                            f"❌ {evt_attrs.get('exception.type', '')}\n"
                            f"{evt_attrs.get('exception.message', '')[:300]}"
                        ),
                        "type": "error",
                    })

        # 5. POST /invocations — response being sent back
        elif name == "POST /invocations" and kind == "SERVER":
            conversation.append({
                "timestamp": timestamp,
                "role": "assistant",
                "content": f"📤 Response sent to user ({duration_ms}ms total processing)",
                "type": "response",
                "duration": duration_ms,
            })

    return conversation


@app.route("/api/sessions/<session_id>/spans")
def get_session_spans(session_id):
    region = request.args.get("region", "us-east-1")
    start_time = int(request.args.get("startTime", int(time.time()) - 2592000))
    end_time = int(request.args.get("endTime", int(time.time())))

    try:
        client = boto3.client("logs", region_name=region)

        query = f"""
            fields @timestamp, @message
            | filter @message like /"{session_id}"/
            | sort @timestamp asc
            | limit 500
        """.strip()

        response = client.start_query(
            logGroupNames=[LOG_GROUPS["spans"]],
            startTime=start_time,
            endTime=end_time,
            queryString=query,
        )
        query_id = response["queryId"]

        # Poll for results
        status = "Running"
        results = []
        while status in ("Running", "Scheduled"):
            time.sleep(1)
            result = client.get_query_results(queryId=query_id)
            status = result.get("status", "Unknown")
            results = result.get("results", [])

        # Parse span JSON from each result row
        spans = []
        for row in results:
            message = ""
            timestamp = ""
            for field in row:
                if field["field"] == "@message":
                    message = field["value"]
                elif field["field"] == "@timestamp":
                    timestamp = field["value"]

            if message:
                try:
                    span_data = json.loads(message)
                    spans.append({
                        "timestamp": timestamp,
                        "traceId": span_data.get("traceId", ""),
                        "spanId": span_data.get("spanId", ""),
                        "parentSpanId": span_data.get("parentSpanId", ""),
                        "name": span_data.get("name", ""),
                        "kind": span_data.get("kind", ""),
                        "startTime": span_data.get("startTimeUnixNano", 0),
                        "endTime": span_data.get("endTimeUnixNano", 0),
                        "durationMs": round(span_data.get("durationNano", 0) / 1_000_000, 2),
                        "status": span_data.get("status", {}).get("code", ""),
                        "attributes": span_data.get("attributes", {}),
                        "events": span_data.get("events", []),
                        "service": span_data.get("resource", {}).get("attributes", {}).get("aws.local.service", ""),
                    })
                except json.JSONDecodeError:
                    continue

        # Sort by startTime
        spans.sort(key=lambda s: s["startTime"])

        return jsonify({"spans": spans, "sessionId": session_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sessions/<session_id>/metrics")
def get_session_metrics(session_id):
    """Compute session-level metrics from spans for developers, PMs, and architects."""
    region = request.args.get("region", "us-east-1")
    start_time = int(request.args.get("startTime", int(time.time()) - 2592000))
    end_time = int(request.args.get("endTime", int(time.time())))

    try:
        spans = load_raw_spans(region, session_id, start_time, end_time)

        # Compute metrics
        total_invocations = 0
        total_llm_calls = 0
        total_tool_calls = 0
        total_gateway_calls = 0
        total_tokens_in = 0
        total_tokens_out = 0
        total_cache_read = 0
        total_cache_write = 0
        total_latency_ms = 0
        invocation_latencies = []
        llm_durations = []
        ttft_values = []
        tool_durations = []
        models_used = set()
        tools_used = {}
        tool_errors = 0
        gateway_durations = []
        event_loop_cycles = 0
        finish_reasons = []
        http_errors = 0

        for s in spans:
            name = s.get("name", "")
            attrs = s.get("attributes", {})
            duration_ms = round(s.get("durationNano", 0) / 1_000_000, 1)

            # Detect invocation: AgentCore native or Strands SDK
            is_invocation = (
                name == "AgentCore.Runtime.Invoke"
                or (name.startswith("invoke_agent") and attrs.get("gen_ai.agent.tools"))
            )

            if is_invocation:
                total_invocations += 1
                latency = attrs.get("latency_ms", duration_ms)
                invocation_latencies.append(latency)
                total_latency_ms += latency
                status = attrs.get("http.response.status_code", attrs.get("http.status_code", 200))
                if int(status) >= 400:
                    http_errors += 1
                # For Strands agents, invoke_agent also carries token/model info
                if "invoke_agent" in name and name != "AgentCore.Runtime.Invoke":
                    total_tokens_in += attrs.get("gen_ai.usage.input_tokens", 0)
                    total_tokens_out += attrs.get("gen_ai.usage.output_tokens", 0)
                    total_cache_read += attrs.get("gen_ai.usage.cache_read_input_tokens", 0)
                    total_cache_write += attrs.get("gen_ai.usage.cache_write_input_tokens", 0)
                    model = attrs.get("gen_ai.request.model", "")
                    if model:
                        models_used.add(model)

            elif "invoke_agent" in name:
                total_tokens_in += attrs.get("gen_ai.usage.input_tokens", 0)
                total_tokens_out += attrs.get("gen_ai.usage.output_tokens", 0)
                total_cache_read += attrs.get("gen_ai.usage.cache_read_input_tokens", 0)
                total_cache_write += attrs.get("gen_ai.usage.cache_write_input_tokens", 0)
                model = attrs.get("gen_ai.request.model", "")
                if model:
                    models_used.add(model)

            elif (name == "chat" or name.startswith("chat ")) and attrs.get("gen_ai.server.time_to_first_token"):
                total_llm_calls += 1
                llm_durations.append(duration_ms)
                ttft = attrs.get("gen_ai.server.time_to_first_token", "")
                if ttft:
                    try:
                        ttft_values.append(float(ttft))
                    except (ValueError, TypeError):
                        pass
                fr = attrs.get("gen_ai.response.finish_reasons", "")
                if fr:
                    finish_reasons.append(fr)

            elif "execute_tool" in name:
                total_tool_calls += 1
                tool_durations.append(duration_ms)
                tool_name = attrs.get("gen_ai.tool.name", "")
                display_name = tool_name.split("___")[-1] if "___" in tool_name else tool_name
                status = attrs.get("gen_ai.tool.status", "")
                if display_name not in tools_used:
                    tools_used[display_name] = {"success": 0, "error": 0, "total_ms": 0}
                tools_used[display_name]["total_ms"] += duration_ms
                if status == "success":
                    tools_used[display_name]["success"] += 1
                else:
                    tools_used[display_name]["error"] += 1
                    tool_errors += 1

            elif name == "execute_event_loop_cycle":
                event_loop_cycles += 1

            elif s.get("kind") == "CLIENT" and ("POST" in name or "GET" in name):
                url = attrs.get("http.url", "")
                if "gateway" in url or "mcp" in url:
                    total_gateway_calls += 1
                    gateway_durations.append(duration_ms)

        # Calculate derived metrics
        avg_latency = total_latency_ms / max(total_invocations, 1)
        avg_llm_duration = sum(llm_durations) / max(len(llm_durations), 1)
        avg_ttft = sum(ttft_values) / max(len(ttft_values), 1) if ttft_values else 0
        p95_ttft = sorted(ttft_values)[int(len(ttft_values) * 0.95)] if len(ttft_values) > 1 else (ttft_values[0] if ttft_values else 0)
        total_llm_time = sum(llm_durations)
        total_tool_time = sum(tool_durations)
        total_gateway_time = sum(gateway_durations)

        # Cost estimate using model-aware pricing
        cost_breakdown = []
        estimated_total_cost = 0
        for model in models_used:
            pricing = get_model_pricing(model)
            # Get tokens for this specific model from invoke_agent spans
            model_tokens_in = 0
            model_tokens_out = 0
            for s in spans:
                if "invoke_agent" in s.get("name", ""):
                    if s.get("attributes", {}).get("gen_ai.request.model") == model:
                        model_tokens_in += s["attributes"].get("gen_ai.usage.input_tokens", 0)
                        model_tokens_out += s["attributes"].get("gen_ai.usage.output_tokens", 0)
            input_cost = (model_tokens_in / 1000) * pricing["input"]
            output_cost = (model_tokens_out / 1000) * pricing["output"]
            total_model_cost = input_cost + output_cost
            estimated_total_cost += total_model_cost
            cost_breakdown.append({
                "model": model,
                "input_tokens": model_tokens_in,
                "output_tokens": model_tokens_out,
                "input_price_per_1k": pricing["input"],
                "output_price_per_1k": pricing["output"],
                "input_cost_usd": round(input_cost, 6),
                "output_cost_usd": round(output_cost, 6),
                "total_usd": round(total_model_cost, 6),
            })

        metrics = {
            "overview": {
                "total_invocations": total_invocations,
                "total_llm_calls": total_llm_calls,
                "total_tool_calls": total_tool_calls,
                "total_gateway_calls": total_gateway_calls,
                "event_loop_cycles": event_loop_cycles,
                "total_spans": len(spans),
                "http_errors": http_errors,
                "tool_errors": tool_errors,
            },
            "latency": {
                "total_session_ms": total_latency_ms,
                "avg_per_invocation_ms": round(avg_latency, 1),
                "invocation_latencies_ms": invocation_latencies,
                "total_llm_time_ms": round(total_llm_time, 1),
                "avg_llm_call_ms": round(avg_llm_duration, 1),
                "total_tool_time_ms": round(total_tool_time, 1),
                "total_gateway_time_ms": round(total_gateway_time, 1),
            },
            "llm": {
                "models_used": list(models_used),
                "total_calls": total_llm_calls,
                "avg_ttft_ms": round(avg_ttft, 1),
                "p95_ttft_ms": round(p95_ttft, 1),
                "finish_reasons": list(set(finish_reasons)),
            },
            "tokens": {
                "total_input": total_tokens_in,
                "total_output": total_tokens_out,
                "total": total_tokens_in + total_tokens_out,
                "cache_read": total_cache_read,
                "cache_write": total_cache_write,
                "avg_input_per_invocation": round(total_tokens_in / max(total_invocations, 1)),
                "avg_output_per_invocation": round(total_tokens_out / max(total_invocations, 1)),
            },
            "tools": {
                "unique_tools_used": list(tools_used.keys()),
                "tool_details": tools_used,
                "total_errors": tool_errors,
            },
            "cost_estimate": {
                "total_usd": round(estimated_total_cost, 6),
                "breakdown": cost_breakdown,
                "note": "Based on AWS Bedrock on-demand pricing per model (token costs only, excludes infrastructure)",
                "source": "https://aws.amazon.com/bedrock/pricing/",
            },
        }

        return jsonify({"metrics": metrics, "sessionId": session_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=3000)
