# Agent Commerce Gateway - Node.js SDK

This is the official server-side Node.js SDK for integrating with the Agent Commerce Gateway. It allows merchants to accept and process Agentic Commerce Protocol (ACP) and x402 requests securely.

## Important Security Warning
**This SDK must only be used on your backend server.** 
Never expose your `sk_test_...` or `sk_live_...` API keys in frontend browser JavaScript (e.g., React, Vue). API keys have full access to your merchant configuration and gateway pipeline.

## Usage Example

```javascript
const AgentCommerceGateway = require('./agent_commerce_gateway');

// 1. Initialize with your Merchant API Key (generated via the Merchant Dashboard)
const gateway = new AgentCommerceGateway("sk_test_YOUR_API_KEY");

async function handleAIProtocolRequest(req, res) {
    const aiPayload = req.body.payload;
    const protocol = req.body.protocol; // e.g., 'acp'

    try {
        // 2. Forward the payload to the gateway for security checks and processing
        const receipt = await gateway.executeRequest(protocol, aiPayload);
        
        // 3. Return the receipt/decision back to the AI buyer
        res.status(200).json(receipt);
        
    } catch (error) {
        console.error("Gateway execution failed:", error.message);
        res.status(error.status || 500).json({ error: error.message });
    }
}
```

## Methods
### `executeRequest(protocol: string, payload: Object): Promise<Object>`
- **`protocol`**: The identifier of the protocol being used (`"acp"`, `"x402"`).
- **`payload`**: The raw protocol request object parsed from JSON.

Throws an error if the gateway returns a non-2xx status (e.g., unauthorized, malformed request, blocked by policy limits). The error object contains `.status` and `.gatewayResponse` for easier debugging.
