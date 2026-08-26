class AgentCommerceGateway {
    /**
     * Initialize the Agent Commerce Gateway SDK.
     * @param {string} apiKey - The merchant's API key (starting with sk_test_)
     * @param {string} gatewayUrl - The URL of the gateway (defaults to http://localhost:8000)
     */
    constructor(apiKey, gatewayUrl = (typeof process !== "undefined" && process.env && process.env.GATEWAY_URL) ? process.env.GATEWAY_URL : "http://localhost:8001") {
        if (!apiKey) {
            throw new Error("AgentCommerceGateway requires an apiKey");
        }
        if (typeof window !== "undefined") {
            console.warn("AgentCommerceGateway should only be used on the server side to protect your API key.");
        }
        this.apiKey = apiKey;
        this.gatewayUrl = gatewayUrl.replace(/\/$/, "");
    }

    /**
     * Verify merchant credentials against Gateway.
     * @returns {Promise<Object>} - Verification status and merchant ID
     */
    async verifyCredentials() {
        const url = `${this.gatewayUrl}/api/v1/verify`;
        const headers = {
            "Authorization": `Bearer ${this.apiKey}`
        };

        const response = await fetch(url, {
            method: "GET",
            headers: headers
        });

        const data = await response.json();
        if (!response.ok) {
            const error = new Error(data.detail || "Authentication verification failed");
            error.status = response.status;
            throw error;
        }
        return data;
    }

    /**
     * Execute an AI Commerce Protocol request.
     * @param {string} protocol - The protocol string (e.g., 'acp', 'x402')
     * @param {Object} payload - The raw protocol payload object
     * @returns {Promise<Object>} - The gateway's decision and receipt
     */
    async executeRequest(protocol, payload) {
        if (!protocol || !payload) {
            throw new Error("protocol and payload are required");
        }

        const url = `${this.gatewayUrl}/api/v1/execute`;
        const headers = {
            "Content-Type": "application/json",
            "Authorization": `Bearer ${this.apiKey}`
        };
        const body = JSON.stringify({
            protocol: protocol,
            raw_payload: payload
        });

        try {
            const response = await fetch(url, {
                method: "POST",
                headers: headers,
                body: body
            });

            const data = await response.json();

            if (!response.ok) {
                // Attach the HTTP status for cleaner error handling upstream
                const error = new Error(data.detail || "Gateway execution failed");
                error.status = response.status;
                error.gatewayResponse = data;
                throw error;
            }

            return data;
        } catch (error) {
            // Re-throw errors so the merchant backend can handle them
            throw error;
        }
    }

    /**
     * Verify and capture client-side completed Razorpay payment.
     * @param {Object} paymentData - { razorpay_order_id, razorpay_payment_id, razorpay_signature, amount_minor, currency }
     * @returns {Promise<Object>} - Capture confirmation
     */
    async verifyPayment(paymentData) {
        if (!paymentData || !paymentData.razorpay_order_id || !paymentData.razorpay_payment_id || !paymentData.razorpay_signature) {
            throw new Error("Missing required payment verification parameters");
        }

        const url = `${this.gatewayUrl}/api/v1/payments/verify`;
        const headers = {
            "Content-Type": "application/json",
            "Authorization": `Bearer ${this.apiKey}`
        };

        const response = await fetch(url, {
            method: "POST",
            headers: headers,
            body: JSON.stringify(paymentData)
        });

        const data = await response.json();
        if (!response.ok) {
            const error = new Error(data.detail || "Payment verification failed");
            error.status = response.status;
            throw error;
        }
        return data;
    }
}

module.exports = AgentCommerceGateway;
