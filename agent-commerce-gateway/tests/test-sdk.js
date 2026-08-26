require("dotenv").config(); // Ensure it loads if running locally
const AgentCommerceGateway = require("../sdk/agent_commerce_gateway");

async function main() {
    const DASHBOARD_SECRET = process.env.DASHBOARD_SECRET;
    if (!DASHBOARD_SECRET) {
        console.log("⚠️ SKIPPING LIVE TESTS: Missing required environment variable DASHBOARD_SECRET.");
        process.exit(0);
    }
    
    const gatewayUrl = "http://localhost:8000";
    console.log("Fetching new API key from local gateway...");
    
    // 1. Fetch an API key
    const apiKeyResponse = await fetch(`${gatewayUrl}/api/dashboard/merchant/api-key`, {
        method: "POST",
        headers: { "X-Dashboard-Token": DASHBOARD_SECRET }
    });
    
    if (!apiKeyResponse.ok) {
        throw new Error(`Failed to get API key: ${apiKeyResponse.status}`);
    }
    
    const { api_key } = await apiKeyResponse.json();
    console.log(`Successfully obtained API key: ${api_key.substring(0, 15)}...`);

    // 2. Initialize SDK
    const gateway = new AgentCommerceGateway(api_key, gatewayUrl);

    // 3. Make a valid ACP request (matching the schema the adapter expects)
    const payload = {
        "merchant_id": "merchant-demo-001", // The default merchant in memory
        "buyer": {"agent_id": "demo-buyer-999"},
        "items": [{"id": "p1", "quantity": 1, "unit_amount": 1000, "currency": "INR"}],
        "idempotency_key": "test-idem-" + Date.now(),
        "bearer_token": "dummy-token"
    };

    console.log("Executing protocol request via SDK...");
    try {
        const result = await gateway.executeRequest("acp", payload);
        console.log("SUCCESS! Gateway responded with receipt:");
        console.log(JSON.stringify(result, null, 2));
    } catch (error) {
        console.error("SDK Request failed with:", error.message);
        if (error.gatewayResponse) {
            console.error(error.gatewayResponse);
        }
        process.exit(1);
    }
}

main().catch(error => {
    console.error(error);
    process.exit(1);
});