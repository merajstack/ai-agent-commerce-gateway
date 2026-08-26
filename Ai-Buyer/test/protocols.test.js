const { getProtocol, listProtocols, generateProtocolPayload, validateX402Payload } = require('../src/protocols');

describe('AI Buyer Protocol Registry & Schema Compliance (ACP & x402 v2)', () => {
  const sampleCart = {
    items: [
      {
        id: 'prod_shoe_001',
        name: 'AeroGlide Runner',
        price: 8500,
        quantity: 1,
        category: 'Running',
        size: '9'
      }
    ],
    total_amount: 8500,
    currency: 'INR'
  };

  test('1. Protocol selector lists ACP and x402 v2 as supported, AP2 as unsupported ("Coming next")', () => {
    const list = listProtocols();
    expect(list.length).toBe(3);
    
    const acp = list.find(p => p.id === 'acp');
    expect(acp).toBeDefined();
    expect(acp.status).toBe('supported');

    const x402 = list.find(p => p.id === 'x402');
    expect(x402).toBeDefined();
    expect(x402.status).toBe('supported');
    expect(x402.version).toBe('v2');

    const ap2 = list.find(p => p.id === 'ap2');
    expect(ap2).toBeDefined();
    expect(ap2.status).toBe('unsupported');
    expect(ap2.description).toContain('Coming next');
  });

  test('2. ACP Generator produces strictly verified RFC 2026-01-16 payload', () => {
    const payload = generateProtocolPayload('acp', sampleCart, {
      buyerIdentity: { agent_id: 'buyer_test_1', first_name: 'Test', last_name: 'User', email: 'test@example.com' },
      merchantId: 'merchant-test-01'
    }).payload;

    expect(payload).toHaveProperty('items');
    expect(payload.items.length).toBe(1);
    expect(payload.items[0]).toEqual({
      id: 'prod_shoe_001',
      name: 'AeroGlide Runner',
      quantity: 1,
      unit_amount: 850000, // 8500 * 100 paise
      currency: 'INR',
      category: 'Running'
    });

    expect(payload.buyer).toEqual({
      agent_id: 'buyer_test_1',
      first_name: 'Test',
      last_name: 'User',
      email: 'test@example.com'
    });

    expect(payload.merchant_id).toBe('merchant-test-01');
    expect(payload.api_version).toBe('2026-01-16');
    expect(payload.idempotency_key).toMatch(/^acp_\d+_[a-z0-9]+$/);
    expect(payload.bearer_token).toMatch(/^acp_token_\d+_[a-z0-9]+$/);
  });

  test('3. x402 v2 Generator produces strictly compliant PaymentPayload schema', () => {
    const res = generateProtocolPayload('x402', sampleCart, {
      buyerIdentity: { agent_id: 'buyer_agent_007', wallet_address: '0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC' },
      merchantId: 'merchant-demo-001',
      payTo: '0x70997970C51812dc3A010C7d01b50e0d17dc79C8'
    });

    expect(res.status).toBe('supported');
    expect(res.protocolId).toBe('x402');
    expect(res.protocolVersion).toBe('v2');

    const p = res.payload;
    expect(p.x402Version).toBe(2);
    expect(p.resource).toBeDefined();
    expect(p.resource.url).toContain('/api/checkout-intent');
    expect(p.resource.mimeType).toBe('application/json');

    // accepted requirements
    expect(p.accepted).toBeDefined();
    expect(p.accepted.scheme).toBe('exact');
    expect(p.accepted.network).toBe('eip155:84532'); // Base Sepolia Testnet
    expect(p.accepted.asset).toBe('0x036CbD53842c5426634e7929541eC2318f3dCF7e');
    expect(p.accepted.amount).toBe('850000'); // 8500 * 100 minor units
    expect(p.accepted.payTo).toBe('0x70997970C51812dc3A010C7d01b50e0d17dc79C8');
    expect(p.accepted.maxTimeoutSeconds).toBe(300);
    expect(p.accepted.extra.merchant_id).toBe('merchant-demo-001');
    expect(p.accepted.extra.buyer_agent_id).toBe('buyer_agent_007');

    // payload authorization & signature
    expect(p.payload).toBeDefined();
    expect(p.payload.authorization.from).toBe('0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC');
    expect(p.payload.authorization.to).toBe('0x70997970C51812dc3A010C7d01b50e0d17dc79C8');
    expect(p.payload.authorization.value).toBe('850000');
    expect(p.payload.authorization.nonce).toMatch(/^0x[a-f0-9]{64}$/);
    expect(p.payload.signature).toMatch(/^0x[a-f0-9]+$/);

    // extensions
    expect(p.extensions).toEqual({});

    // Validates against validator function
    expect(validateX402Payload(p)).toBe(true);
  });

  test('4. AP2 remains unsupported with explicit "Coming next" status notice', () => {
    const resAP2 = generateProtocolPayload('ap2', sampleCart);
    expect(resAP2.status).toBe('unsupported');
    expect(resAP2.payload).toBeNull();
    expect(resAP2.error).toContain('Coming next');
  });

  test('5. Rejects malformed x402 payloads strictly', () => {
    // Missing x402Version
    expect(() => validateX402Payload({
      resource: { url: 'http://test' },
      accepted: { scheme: 'exact', network: 'eip155:84532', asset: '0x1', amount: '100', payTo: '0x2' },
      payload: { authorization: {}, signature: '0x123' },
      extensions: {}
    })).toThrow(/x402Version must be integer 2/);

    // Wrong x402Version (e.g. 1)
    expect(() => validateX402Payload({
      x402Version: 1,
      resource: { url: 'http://test' },
      accepted: { scheme: 'exact', network: 'eip155:84532', asset: '0x1', amount: '100', payTo: '0x2' },
      payload: { authorization: {}, signature: '0x123' },
      extensions: {}
    })).toThrow(/x402Version must be integer 2/);

    // Missing resource.url
    expect(() => validateX402Payload({
      x402Version: 2,
      resource: {},
      accepted: { scheme: 'exact', network: 'eip155:84532', asset: '0x1', amount: '100', payTo: '0x2' },
      payload: { authorization: {}, signature: '0x123' },
      extensions: {}
    })).toThrow(/resource object with a valid "url"/);

    // Missing accepted.scheme
    expect(() => validateX402Payload({
      x402Version: 2,
      resource: { url: 'http://test' },
      accepted: { network: 'eip155:84532', asset: '0x1', amount: '100', payTo: '0x2' },
      payload: { authorization: {}, signature: '0x123' },
      extensions: {}
    })).toThrow(/accepted.scheme string is required/);

    // Missing signature
    expect(() => validateX402Payload({
      x402Version: 2,
      resource: { url: 'http://test' },
      accepted: { scheme: 'exact', network: 'eip155:84532', asset: '0x1', amount: '100', payTo: '0x2' },
      payload: { authorization: {} },
      extensions: {}
    })).toThrow(/payload.signature hex string is required/);
  });

  test('6. Zero secret exposure: x402 and ACP payloads contain NO keys or secrets', () => {
    const x402Payload = generateProtocolPayload('x402', sampleCart).payload;
    const acpPayload = generateProtocolPayload('acp', sampleCart).payload;

    [x402Payload, acpPayload].forEach(payload => {
      const serialized = JSON.stringify(payload).toLowerCase();
      expect(serialized).not.toContain('razorpay_key_secret');
      expect(serialized).not.toContain('key_secret');
      expect(serialized).not.toContain('api_key');
      expect(serialized).not.toContain('private_key');
    });
  });

  test('7. Rejects empty cart for x402 generation gracefully', () => {
    const x402 = getProtocol('x402');
    expect(() => x402.generateRequest({ items: [] })).toThrow('Cart is empty');
  });
});
