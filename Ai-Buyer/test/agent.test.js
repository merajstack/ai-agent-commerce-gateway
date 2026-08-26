const gemini = require('../src/gemini');
const toolHandlers = require('../src/tools/handlers');

describe('AI Buyer Multi-Turn Conversation, ACP Execution, x402 v2 Flow & Payment Verification', () => {
  let checkoutCallCount = 0;
  let verifyPaymentCallCount = 0;
  let addedCartItems = [];

  beforeEach(() => {
    checkoutCallCount = 0;
    verifyPaymentCallCount = 0;
    addedCartItems = [];

    // Mock global fetch
    global.fetch = jest.fn((url, options) => {
      const urlStr = url.toString();

      // Gemini Google API (reject to test deterministic planner or offline mode)
      if (urlStr.includes('googleapis.com')) {
        return Promise.reject(new Error('Test mode: offline planner active'));
      }

      // 1. Storefront catalog
      if (urlStr.includes('/api/products')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({
            count: 5,
            data: [
              { id: 'prod_shoe_001', name: 'AeroGlide Runner', price: 8500, category: 'Running', stock: 45, sizes: ['7', '8', '9', '10', '11'] },
              { id: 'prod_shoe_002', name: 'Urban Kicks Classic', price: 5200, category: 'Casual', stock: 120, sizes: ['6', '7', '8', '9'] },
              { id: 'prod_shoe_005', name: 'Slip-On Comfort', price: 3500, category: 'Casual', stock: 200, sizes: ['7', '8', '9'] },
              { id: 'prod_shoe_007', name: 'Leather Oxford Elite', price: 14500, category: 'Formal', stock: 25, sizes: ['8', '9', '10'] },
              { id: 'prod_shoe_008', name: 'Aqua Walker', price: 4200, category: 'Outdoor', stock: 85, sizes: ['7', '8', '9', '10'] }
            ]
          })
        });
      }

      // 2. Cart GET
      if (urlStr.includes('/api/cart') && (!options || options.method !== 'POST')) {
        const total = addedCartItems.reduce((sum, item) => sum + item.price * item.quantity, 0);
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({
            data: {
              items: addedCartItems,
              total_amount: total,
              currency: 'INR'
            }
          })
        });
      }

      // 3. Cart POST (add/update)
      if (urlStr.includes('/api/cart') && options && options.method === 'POST') {
        const body = JSON.parse(options.body);
        const nameMap = {
          'prod_shoe_001': 'AeroGlide Runner',
          'prod_shoe_002': 'Urban Kicks Classic',
          'prod_shoe_005': 'Slip-On Comfort',
          'prod_shoe_007': 'Leather Oxford Elite',
          'prod_shoe_008': 'Aqua Walker'
        };
        const priceMap = {
          'prod_shoe_001': 8500,
          'prod_shoe_002': 5200,
          'prod_shoe_005': 3500,
          'prod_shoe_007': 14500,
          'prod_shoe_008': 4200
        };

        const newItem = {
          id: body.id,
          name: nameMap[body.id] || 'AeroGlide Runner',
          price: priceMap[body.id] || 8500,
          quantity: body.quantity || 1,
          size: body.size || '9',
          category: 'Running'
        };

        addedCartItems = [newItem];

        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({
            data: {
              items: addedCartItems,
              total_amount: newItem.price * newItem.quantity,
              currency: 'INR'
            }
          })
        });
      }

      // 4. ACP Checkout intent (Forward raw ACP payload to Gateway /api/v1/execute)
      if (urlStr.includes('/api/checkout-intent')) {
        checkoutCallCount++;
        const body = JSON.parse(options.body);
        
        expect(['acp', 'x402']).toContain(body.protocol);
        expect(body.payload).toBeDefined();

        const totalPaise = addedCartItems.reduce((sum, item) => sum + item.price * item.quantity * 100, 0);

        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({
            success: true,
            data: {
              gateway_decision: 'ALLOW',
              protocol: body.protocol,
              transaction_id: `${body.protocol}-txn-test-001`,
              razorpay_order_id: 'order_test_aeroglide_3x',
              razorpay_key_id: 'rzp_test_TSuG9gfvyjCsK2',
              amount_minor: totalPaise || 2550000,
              currency: 'INR',
              canonical_request: { total: { amount_minor: totalPaise || 2550000, currency: 'INR' } },
              pipeline_stages: [
                { stage: 'REQUEST', name: 'Incoming Protocol Request', status: 'PASSED' },
                { stage: 'PROTOCOL_ADAPTER', name: 'Canonical Normalization', status: 'PASSED' },
                { stage: 'AUTHORIZATION', name: 'Authorization Check', status: 'PASSED' },
                { stage: 'REPLAY', name: 'Anti-Replay Defense', status: 'PASSED' },
                { stage: 'POLICY', name: 'Merchant Policy Engine', status: 'PASSED' },
                { stage: 'DECISION', name: 'Gateway Final Decision', status: 'ALLOW' },
                { stage: 'RAZORPAY_ORDER', name: 'Razorpay Test Mode Order', status: 'PASSED' }
              ]
            }
          })
        });
      }

      // 5. Verify payment (Storefront / Gateway /api/v1/payments/verify)
      if (urlStr.includes('/api/verify-payment')) {
        verifyPaymentCallCount++;
        const body = JSON.parse(options.body);

        expect(body.razorpay_order_id).toBeDefined();
        expect(body.razorpay_payment_id).toBeDefined();
        expect(body.razorpay_signature).toBeDefined();

        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({
            success: true,
            data: {
              success: true,
              execution_status: 'captured',
              razorpay_order_id: body.razorpay_order_id,
              razorpay_payment_id: body.razorpay_payment_id,
              razorpay_payment_status: 'captured'
            }
          })
        });
      }

      return Promise.reject(new Error(`Unhandled mock URL: ${urlStr}`));
    });
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  test('1. Price-only request → no Gateway call, no ACP/x402 request, calculates 3x price (₹25,500)', async () => {
    const message = 'I need 3 AeroGlide Runner, tell me the price before buying';
    const result = await gemini.processMessage(message, [], 'acp');

    expect(result.message).toContain('25,500');
    expect(result.message).toContain('AeroGlide Runner');
    expect(result.message).toContain('3');
    expect(result.rawProtocolRequest).toBeNull();
    expect(result.gatewayResponse).toBeNull();
    expect(checkoutCallCount).toBe(0);
  });

  test('2. ACP Confirmation → Gateway called once, ACP generated with quantity 3 & returns Razorpay key and order', async () => {
    const history = [
      { role: 'user', content: 'Get 3 AeroGlide Runner, tell me the total.' },
      { role: 'assistant', content: 'The AeroGlide Runner is ₹8,500. For 3 pairs, the total is ₹25,500. Would you like me to proceed with the purchase?' }
    ];

    const message = 'Okay, buy them in size 9.';
    const result = await gemini.processMessage(message, history, 'acp');

    expect(checkoutCallCount).toBe(1);
    expect(result.gatewayResponse).toBeDefined();
    expect(result.gatewayResponse.gateway_decision).toBe('ALLOW');
    expect(result.gatewayResponse.razorpay_order_id).toBe('order_test_aeroglide_3x');
    expect(result.gatewayResponse.razorpay_key_id).toBe('rzp_test_TSuG9gfvyjCsK2');

    expect(result.rawProtocolRequest).toBeDefined();
    expect(result.rawProtocolRequest.items).toHaveLength(1);
    expect(result.rawProtocolRequest.items[0].id).toBe('prod_shoe_001');
    expect(result.rawProtocolRequest.items[0].name).toBe('AeroGlide Runner');
    expect(result.rawProtocolRequest.items[0].quantity).toBe(3);
    expect(result.rawProtocolRequest.items[0].unit_amount).toBe(850000);
  });

  test('3. x402 v2 Flow → Generates genuine PaymentPayload (x402Version: 2), normalizes via Gateway and reaches ALLOW', async () => {
    addedCartItems = [
      { id: 'prod_shoe_008', name: 'Aqua Walker', price: 4200, quantity: 1, category: 'Outdoor', size: '9' }
    ];

    const message = 'Buy Aqua Walker with x402';
    const result = await gemini.processMessage(message, [], 'x402');

    expect(result.rawProtocolRequest).toBeDefined();
    const p = result.rawProtocolRequest;
    expect(p.x402Version).toBe(2);
    expect(p.accepted.scheme).toBe('exact');
    expect(p.accepted.network).toBe('eip155:84532');
    expect(p.accepted.amount).toBe('420000'); // 4200 * 100 paise
    expect(p.payload.signature).toMatch(/^0x[a-f0-9]+$/);

    // Normalizes through Gateway
    expect(result.gatewayResponse).toBeDefined();
    expect(result.gatewayResponse.gateway_decision).toBe('ALLOW');
    expect(result.message).toContain('x402 v2 Order Authorized & Prepared!');
    expect(result.message).toContain('Base Sepolia');
    expect(result.message).toContain('exact');
  });

  test('4. Product & quantity preserved across turns without silent substitution', async () => {
    const history = [
      { role: 'user', content: 'I need 3 AeroGlide Runner, tell me the price before buying' },
      { role: 'assistant', content: 'The AeroGlide Runner is priced at ₹8,500 per pair. For 3 pairs, the total is ₹25,500.' }
    ];

    const message = 'Okay, buy them in size 9';
    const result = await gemini.processMessage(message, history, 'acp');

    expect(result.message).toContain('AeroGlide Runner');
    expect(result.message).not.toContain('Leather Oxford Elite');
    expect(result.rawProtocolRequest.items[0].name).toBe('AeroGlide Runner');
    expect(result.rawProtocolRequest.items[0].quantity).toBe(3);
  });

  test('5. Payment verification forwards HMAC signature to Gateway /api/v1/payments/verify and captures', async () => {
    const verifyResult = await toolHandlers.verifyPayment({
      razorpay_order_id: 'order_test_aeroglide_3x',
      razorpay_payment_id: 'pay_test_123456789',
      razorpay_signature: 'test_hmac_sha256_signature',
      amount_minor: 2550000,
      currency: 'INR'
    });

    expect(verifyPaymentCallCount).toBe(1);
    expect(verifyResult.success).toBe(true);
    expect(verifyResult.data.execution_status).toBe('captured');
    expect(verifyResult.data.razorpay_payment_id).toBe('pay_test_123456789');
  });

  test('6. AP2 remains unsupported with explicit notice', async () => {
    const res = await toolHandlers.executeCheckout('ap2');
    expect(res.success).toBe(false);
    expect(res.status_notice).toBe('PROTOCOL_UNSUPPORTED');
    expect(res.raw_protocol_payload).toBeNull();
    expect(res.error).toContain('Coming next');
  });

  test('7. Conversational greeting ("hey how are you") with x402 → polite answer only, no product recs, no protocol request', async () => {
    const message = 'hey how are you';
    const result = await gemini.processMessage(message, [], 'x402');

    expect(result.message).toBe("I’m doing well! What would you like to shop for?");
    expect(result.rawProtocolRequest).toBeNull();
    expect(result.gatewayResponse).toBeNull();
    expect(checkoutCallCount).toBe(0);
  });

  test('8. Search/Browse ("Find running shoes under ₹5000") with x402 → product catalog list only, no protocol request, no Gateway call', async () => {
    const message = 'Find running shoes under ₹5000';
    const result = await gemini.processMessage(message, [], 'x402');

    expect(result.message).toContain('Running Shoes under ₹5,000');
    expect(result.message).toContain('Aqua Walker');
    expect(result.message).toContain('₹4,200');
    expect(result.rawProtocolRequest).toBeNull();
    expect(result.gatewayResponse).toBeNull();
    expect(checkoutCallCount).toBe(0);
  });

  test('9. General talk ("tell me a joke") → conversational humor without triggering checkout', async () => {
    const message = 'tell me a joke';
    const result = await gemini.processMessage(message, [], 'acp');

    expect(result.message.length).toBeGreaterThan(15);
    expect(result.rawProtocolRequest).toBeNull();
    expect(result.gatewayResponse).toBeNull();
    expect(checkoutCallCount).toBe(0);
  });

  test('10. Store technical query ("which shoes are waterproof?") → returns Trail Blazer GTX & Aqua Walker specs', async () => {
    const message = 'which shoes are waterproof?';
    const result = await gemini.processMessage(message, [], 'acp');

    expect(result.message).toContain('Trail Blazer GTX');
    expect(result.message).toContain('Aqua Walker');
    expect(result.message).toContain('Gore-Tex');
    expect(result.rawProtocolRequest).toBeNull();
    expect(result.gatewayResponse).toBeNull();
    expect(checkoutCallCount).toBe(0);
  });

  test('11. Store policy query ("what is your return policy?") → returns 30-day policy & shipping info', async () => {
    const message = 'what is your return policy?';
    const result = await gemini.processMessage(message, [], 'acp');

    expect(result.message).toContain('30-Day Easy Returns');
    expect(result.message).toContain('Express Shipping');
    expect(result.rawProtocolRequest).toBeNull();
    expect(result.gatewayResponse).toBeNull();
    expect(checkoutCallCount).toBe(0);
  });
});
