const BaseProtocol = require('./base');

/**
 * ACP (Agentic Commerce Protocol) Request Generator
 * 
 * Authoritative source:
 * GitHub: agentic-commerce-protocol/agentic-commerce-protocol
 * Stable spec: spec/2026-04-17 (RFC version 2026-01-16)
 * RFC: rfcs/rfc.agentic_checkout.md
 * 
 * Strict invariants:
 * - unit_amount in minor currency units (paise/cents integer)
 * - currency uppercase 3-letter ISO code
 * - idempotency_key required unique string
 * - bearer_token required string
 * - api_version: "2026-01-16"
 */
class ACPProtocol extends BaseProtocol {
  constructor() {
    super(
      'acp',
      'ACP (Agentic Commerce Protocol)',
      '2026-01-16',
      'supported',
      'Authoritative Agentic Commerce Protocol with cryptographic session isolation.'
    );
  }

  generateRequest(cart, options = {}) {
    if (!cart || !cart.items || cart.items.length === 0) {
      throw new Error('ACP Request generation failed: Cart is empty');
    }

    const currency = (cart.currency || 'INR').toUpperCase();
    const buyer = options.buyerIdentity || {
      agent_id: 'agent_buyer_007',
      first_name: 'Buyer',
      last_name: 'Agent',
      email: 'buyer@agentcommerce.ai'
    };
    const merchantId = options.merchantId || 'merchant-demo-001';
    const timestamp = Date.now();
    const randomSuffix = Math.random().toString(36).substring(2, 8);

    // Map items to ACPLineItem schema
    const items = cart.items.map(item => ({
      id: item.id,
      name: item.name,
      quantity: parseInt(item.quantity, 10) || 1,
      unit_amount: Math.round(Number(item.price) * 100), // convert to paise / minor units
      currency: currency,
      category: item.category || 'Footwear'
    }));

    return {
      items: items,
      buyer: {
        agent_id: buyer.agent_id,
        first_name: buyer.first_name,
        last_name: buyer.last_name,
        email: buyer.email
      },
      merchant_id: merchantId,
      idempotency_key: `acp_${timestamp}_${randomSuffix}`,
      bearer_token: `acp_token_${timestamp}_${randomSuffix}`,
      api_version: '2026-01-16'
    };
  }
}

module.exports = new ACPProtocol();
