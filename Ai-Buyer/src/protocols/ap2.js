const BaseProtocol = require('./base');

/**
 * AP2 (Agent Payment Protocol) Draft Request Generator
 * 
 * Notice:
 * AP2 is currently an emerging draft protocol.
 * This generator constructs the formal draft payload according to the AP2 working group specification.
 * It is clearly flagged with status 'experimental' so the Gateway and UI do not fabricate production compliance.
 */
class AP2Protocol extends BaseProtocol {
  constructor() {
    super(
      'ap2',
      'AP2 (Agent Payment Protocol)',
      'v1.0.0-draft',
      'experimental',
      'Emerging mandate-driven autonomous payment protocol (Draft Specification).'
    );
  }

  generateRequest(cart, options = {}) {
    if (!cart || !cart.items || cart.items.length === 0) {
      throw new Error('AP2 Request generation failed: Cart is empty');
    }

    const currency = (cart.currency || 'INR').toUpperCase();
    const buyer = options.buyerIdentity || { agent_id: 'agent_buyer_007' };
    const merchantId = options.merchantId || 'merchant-demo-001';
    const totalMinor = cart.items.reduce((sum, item) => sum + Math.round(Number(item.price) * Number(item.quantity) * 100), 0);

    const items = cart.items.map(item => ({
      item_id: item.id,
      name: item.name,
      units: parseInt(item.quantity, 10) || 1,
      unit_cost_minor: Math.round(Number(item.price) * 100)
    }));

    return {
      protocol_version: 'ap2/v1.0.0-draft',
      mandate: {
        mandate_id: `man_ap2_${Date.now()}`,
        delegated_buyer_agent: buyer.agent_id,
        target_merchant_id: merchantId,
        authorized_ceiling_minor: totalMinor,
        currency: currency,
        expires_at: new Date(Date.now() + 3600000).toISOString()
      },
      payment_intent: {
        line_items: items,
        total_authorized_minor: totalMinor,
        currency: currency,
        intent_nonce: `nonce_${Math.random().toString(36).substring(2, 10)}`
      },
      status_notice: 'EXPERIMENTAL_DRAFT'
    };
  }
}

module.exports = new AP2Protocol();
