# Protecting AskLit with Azure API Management

This setup gives a twelve-person educator cohort one limited Azure API
Management (APIM) key and one pooled quota. It never gives an educator an Azure
AI Services account key. APIM authenticates to
Azure AI with its managed identity, and local/key authentication on the Azure AI
account is disabled.

## What is deployed

| Resource | Name | Purpose |
| --- | --- | --- |
| Resource group | `tulane-ai-rg` | Cohort resources and the $20 monthly budget |
| Azure AI Services | `tulane-ai-eastus2` | Hosts the model deployments |
| APIM Consumption gateway | `tulane-asklit-gateway` | Validates educator keys and enforces limits |
| APIM API | `asklit-educator` | Exposes only `POST /asklit/chat/completions` |
| APIM product | `asklit-educator-trial` | Applies a quota independently to each subscription key |
| APIM subscription | `educator-cohort` | One shared key and pooled quota for twelve educators |

The built-in service-wide `master` subscription is suspended so it cannot
bypass the product quota.

The gateway URL is:

```text
https://tulane-asklit-gateway.azure-api.net/asklit
```

The approved chat deployment names are:

```text
gpt-5.4-nano
gpt-5.4-mini
gpt-5.6-sol
deepseek-v4-pro
grok-4.1-fast-reasoning
llama-4-maverick
```

The FLUX image deployment was removed and is not exposed.

## Why Consumption tier works here

APIM Consumption supports the `rate-limit` and `quota` policies. At product
scope, both counters are maintained separately for each APIM subscription key.
It does **not** support `llm-token-limit`, `rate-limit-by-key`, or
`quota-by-key`. Those policies require a paid APIM tier.

For this pilot, the shared cohort subscription currently receives:

- 30 calls per minute
- 600 calls per 2,592,000-second (30-day) subscription window
- 150,000 KB aggregate request/response bandwidth in that window
- 200,000 characters per serialized request body
- 4,000 requested output tokens per call
- access to the six approved chat deployments only

The quota window is a rolling APIM subscription period, not a calendar month.
The $20 Azure budget remains calendar-month based. Consumption APIM avoids the
fixed Basic v2 charge, but model inference is still billed normally.

This is a disclosure-containment control: a stolen key can make at most the
pool's remaining 600 calls, can be revoked immediately, and cannot call the
Azure AI account directly. The Azure budget alert is only a delayed backstop.
Because the key is shared, one educator can consume the pool and a rotation
requires updating all twelve educators.

Microsoft policy references:

- [APIM rate-limit policy](https://learn.microsoft.com/azure/api-management/rate-limit-policy)
- [APIM quota policy](https://learn.microsoft.com/azure/api-management/quota-policy)
- [LLM token limit policy and tier availability](https://learn.microsoft.com/azure/api-management/llm-token-limit-policy)
- [APIM pricing](https://azure.microsoft.com/pricing/details/api-management/)

## AskLit integration

AskLit's `azure_apim` provider sends its secret in
`Ocp-Apim-Subscription-Key`. It uses the gateway as an OpenAI-compatible base
URL. The application also applies a server-side output ceiling and validates
model selections against its configured allowlist.

The chat sidebar can expose an approved-model selector. This is useful for the
workshop, but it is not the security boundary: APIM repeats the allowlist and
output validation if someone bypasses AskLit and calls the gateway directly.

Set these values in Streamlit Secrets or in ignored
`.streamlit/secrets.toml` for local development:

```toml
AZURE_APIM_API_KEY = "<shared cohort APIM subscription key>"
AZURE_APIM_BASE_URL = "https://tulane-asklit-gateway.azure-api.net/asklit"

"model.provider" = "azure_apim"
"model.name" = "gpt-5.4-nano"
"model.allow_user_selection" = "true"
"model.allowed_models" = "gpt-5.4-nano,gpt-5.4-mini,gpt-5.6-sol,deepseek-v4-pro,grok-4.1-fast-reasoning,llama-4-maverick"
"model.max_tokens" = "4000"
"limits.max_output_tokens_hard" = "4000"
"limits.max_conversation_turns" = "30"
"model.use_local_embeddings" = "true"
```

Use local embeddings for this pilot. The gateway intentionally exposes no
embedding operation or embedding deployment.

## Retrieve and distribute the cohort key

Sign in to the intended Azure subscription, then retrieve the shared key:

```bash
az login
./scripts/get-cohort-key.sh
```

The script reads the secret from Azure and prints it once; it does not save it.
Put it directly into the training app's Streamlit Secrets, or distribute it
through an approved password manager or other encrypted channel. Never commit
it, paste it into an issue, or put it in slides, handouts, or a group chat.

An APIM subscription key is still a bearer credential. It is “safe to share”
with the cohort only because its permissions and maximum exposure are bounded.

To stop the shared key immediately:

```bash
az rest --method patch \
  --url "https://management.azure.com/subscriptions/<subscription-id>/resourceGroups/tulane-ai-rg/providers/Microsoft.ApiManagement/service/tulane-asklit-gateway/subscriptions/educator-cohort?api-version=2024-05-01" \
  --headers Content-Type=application/json \
  --body '{"properties":{"state":"suspended"}}'
```

To rotate a disclosed key, regenerate the cohort subscription's primary key in
**APIM > Subscriptions**, update the training app or redistribute the
replacement, and verify that the old key receives HTTP 401.

## Reproduce the Azure architecture

Create a Consumption APIM instance with a managed identity:

```bash
az apim create \
  --resource-group tulane-ai-rg \
  --name tulane-asklit-gateway \
  --location eastus2 \
  --sku-name Consumption \
  --enable-managed-identity true \
  --publisher-email <administrator-email> \
  --publisher-name <organization-name>
```

Grant only `Cognitive Services User` to the APIM identity, scoped to the Azure
AI account. Create an HTTP API with backend URL
`https://tulane-ai-eastus2.openai.azure.com/openai/v1`, require a subscription,
and create only a POST operation at `/chat/completions`.

Apply this policy to the API. Create the non-secret APIM named value
`asklit-gateway-enabled=true` first.

```xml
<policies>
  <inbound>
    <base />
    <choose>
      <when condition="@(!bool.Parse(&quot;{{asklit-gateway-enabled}}&quot;))">
        <return-response>
          <set-status code="403" reason="Gateway disabled" />
          <set-body>{"error":{"message":"The AskLit educator gateway is temporarily disabled."}}</set-body>
        </return-response>
      </when>
    </choose>
    <set-variable name="requestLength"
      value="@((context.Request.Body.As&lt;string&gt;(preserveContent: true) ?? string.Empty).Length)" />
    <choose>
      <when condition="@((int)context.Variables[&quot;requestLength&quot;] &gt; 200000)">
        <return-response><set-status code="413" reason="Request too large" /></return-response>
      </when>
    </choose>
    <set-variable name="requestAllowed" value="@{
      try {
        var body = context.Request.Body.As&lt;Newtonsoft.Json.Linq.JObject&gt;(preserveContent: true);
        if (body == null) { return false; }
        var model = (string)body[&quot;model&quot;];
        var modelAllowed =
          model == &quot;gpt-5.4-nano&quot; || model == &quot;gpt-5.4-mini&quot; ||
          model == &quot;gpt-5.6-sol&quot; || model == &quot;deepseek-v4-pro&quot; ||
          model == &quot;grok-4.1-fast-reasoning&quot; || model == &quot;llama-4-maverick&quot;;
        var maximum = (int?)body[&quot;max_completion_tokens&quot;] ??
          (int?)body[&quot;max_tokens&quot;];
        return modelAllowed &amp;&amp; maximum.HasValue &amp;&amp;
          maximum.Value &gt; 0 &amp;&amp; maximum.Value &lt;= 4000 &amp;&amp;
          body[&quot;messages&quot;] != null;
      } catch { return false; }
    }" />
    <choose>
      <when condition="@(!(bool)context.Variables[&quot;requestAllowed&quot;])">
        <return-response><set-status code="403" reason="Request not allowed" /></return-response>
      </when>
    </choose>
    <set-header name="api-key" exists-action="delete" />
    <authentication-managed-identity resource="https://cognitiveservices.azure.com" />
  </inbound>
  <backend><base /></backend>
  <outbound><base /></outbound>
  <on-error><base /></on-error>
</policies>
```

Apply the following policy to a published, subscription-required product. Add
the API to that product and create one `educator-cohort` APIM subscription.

```xml
<policies>
  <inbound>
    <base />
    <rate-limit calls="30" renewal-period="60" />
    <quota calls="600" bandwidth="150000" renewal-period="2592000" />
  </inbound>
  <backend><base /></backend>
  <outbound><base /></outbound>
  <on-error><base /></on-error>
</policies>
```

Suspend APIM's built-in all-access subscription after creating and testing the
cohort subscription; otherwise that administrative key bypasses the product
quota.

After a successful gateway test, regenerate both Azure AI account keys and set
`disableLocalAuth=true` permanently. The deployed setup has already completed
this step. The APIM managed identity continues working; an Azure AI account key
does not.

## Budget backstop and global kill switch

The resource-group budget `tulane-ai-monthly-20` sends informational alerts at
80%, 100%, and forecast thresholds. Only the 100% actual-cost threshold invokes
the `tulane-budget-shutdown` Action Group.

That Action Group calls a managed-identity Logic App which changes only the
APIM named value `asklit-gateway-enabled` to `false`. A second workflow restores
it to `true` at 00:05 Eastern on the first day of each month. Neither workflow
can enable Azure AI account-key authentication.

Budget data can lag by many hours. The per-key APIM policy is the immediate
abuse control; the budget workflow is not.

## Verification checklist

- Missing, invalid, or cancelled educator key: HTTP 401
- Unapproved model or output request above 4,000 tokens: HTTP 403
- Request body above 200,000 characters: HTTP 413
- More than 30 calls in a minute: HTTP 429
- Per-key call or bandwidth quota exhausted: HTTP 403
- Direct Azure AI request with an account key: HTTP 401
- All six approved model deployments through APIM: HTTP 200
- AskLit displays only a generic provider/limit error to non-admin users

APIM counters can overshoot slightly when concurrent requests arrive at
exactly the same time. Keep the quota below the absolute maximum exposure you
would tolerate, monitor APIM analytics, and cancel a key immediately when an
educator reports disclosure.
