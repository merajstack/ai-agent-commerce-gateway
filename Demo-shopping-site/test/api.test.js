const request = require('supertest');
const app = require('../server'); // Import Express app

// Mock the Gateway SDK to avoid actual network calls during tests
const mockSdkInstance = {
  verifyCredentials: jest.fn().mockResolvedValue({ valid: true, merchant_id: 'merchant-demo-001' }),
  executeRequest: jest.fn().mockResolvedValue({
    gateway_decision: 'ALLOW',
    status: 'ready_for_payment',
    transaction_id: 'acp-test-123',
    razorpay_order_id: 'order_test_123',
    razorpay_key_id: 'rzp_test_mock',
    amount_minor: 850000,
    currency: 'INR'
  }),
  verifyPayment: jest.fn().mockResolvedValue({
    success: true,
    execution_status: 'payment_captured',
    razorpay_order_id: 'order_test_123',
    razorpay_payment_id: 'pay_test_123'
  })
};

jest.mock('../../agent-commerce-gateway/sdk/agent_commerce_gateway.js', () => {
  return jest.fn().mockImplementation(() => mockSdkInstance);
});

describe('AI-Friendly Commerce APIs', () => {
  beforeEach(async () => {
    // Reset cart before each test
    await request(app).post('/api/test/reset-cart');
  });

  test('1. Agent can search/query products via API', async () => {
    // Query by category
    const resCat = await request(app).get('/api/products?category=Running');
    expect(resCat.statusCode).toBe(200);
    expect(resCat.body.data.length).toBeGreaterThan(0);
    expect(resCat.body.data[0].category).toBe('Running');

    // Query by text search
    const resSearch = await request(app).get('/api/products?search=Sprint');
    expect(resSearch.statusCode).toBe(200);
    expect(resSearch.body.data.length).toBeGreaterThan(0);
    expect(resSearch.body.data[0].name).toContain('Sprint');
  });

  test('2. Agent can compare price/category/stock by fetching structured JSON', async () => {
    const res = await request(app).get('/api/products');
    expect(res.statusCode).toBe(200);
    expect(res.body.data.length).toBe(10); // 10 products

    const product = res.body.data[0];
    // Check for explicit structured fields required by AI
    expect(product).toHaveProperty('id');
    expect(product).toHaveProperty('name');
    expect(product).toHaveProperty('price');
    expect(product).toHaveProperty('currency');
    expect(product).toHaveProperty('category');
    expect(product).toHaveProperty('description');
    expect(product).toHaveProperty('stock');
    expect(product).toHaveProperty('sizes');
    expect(product).toHaveProperty('availability');
    expect(typeof product.price).toBe('number');
  });

  test('3. Agent can retrieve one product for complete details', async () => {
    const productId = 'prod_shoe_001';
    const res = await request(app).get(`/api/products/${productId}`);
    expect(res.statusCode).toBe(200);
    expect(res.body.data.id).toBe(productId);
    expect(res.body.data.name).toBe('AeroGlide Runner');
  });

  test('4 & 5. Agent can add to cart and change quantity', async () => {
    const productId = 'prod_shoe_002'; // Urban Kicks Classic

    // 4. Add to cart
    const addRes = await request(app)
      .post('/api/cart')
      .send({ id: productId, quantity: 1, size: '8' });
    
    expect(addRes.statusCode).toBe(200);
    expect(addRes.body.data.items.length).toBe(1);
    expect(addRes.body.data.items[0].id).toBe(productId);
    expect(addRes.body.data.items[0].quantity).toBe(1);

    // 5. Change quantity
    const updateRes = await request(app)
      .post('/api/cart')
      .send({ id: productId, quantity: 3, size: '8' });
    
    expect(updateRes.statusCode).toBe(200);
    expect(updateRes.body.data.items[0].quantity).toBe(3);
    
    // Check if total amount is calculated correctly
    // Price of prod_shoe_002 is 5200. 5200 * 3 = 15600
    expect(updateRes.body.data.total_amount).toBe(15600);
  });

  test('6. Agent can request checkout (Gateway SDK Integration)', async () => {
    // Set connected mock runtime config
    await request(app).post('/api/test/set-config').send({ 
      apiKey: 'sk_test_demo123', 
      connected: true, 
      merchantId: 'merchant-demo-001' 
    });

    // Add item first
    await request(app)
      .post('/api/cart')
      .send({ id: 'prod_shoe_001', quantity: 1 });

    // Request checkout
    const checkoutRes = await request(app).post('/api/checkout-intent');
    
    expect(checkoutRes.statusCode).toBe(200);
    expect(checkoutRes.body.success).toBe(true);
    expect(checkoutRes.body.data.intent_status).toBe('created');
    expect(checkoutRes.body.data.gateway_decision).toBe('ALLOW');
    expect(checkoutRes.body.data.razorpay_order_id).toBe('order_test_123');
    
    // Check if cart is cleared after intent
    const emptyCheck = await request(app).post('/api/cart').send({ id: 'prod_shoe_001', quantity: 0 });
    expect(emptyCheck.body.data.items.length).toBe(0);
  });

  test('7. Payment verification endpoint forwards to Gateway', async () => {
    await request(app).post('/api/test/set-config').send({ 
      apiKey: 'sk_test_demo123', 
      connected: true, 
      merchantId: 'merchant-demo-001' 
    });

    const res = await request(app)
      .post('/api/verify-payment')
      .send({
        razorpay_order_id: 'order_test_123',
        razorpay_payment_id: 'pay_test_123',
        razorpay_signature: 'sig_123',
        amount_minor: 850000,
        currency: 'INR'
      });

    expect(res.statusCode).toBe(200);
    expect(res.body.success).toBe(true);
    expect(res.body.data.execution_status).toBe('payment_captured');
  });
});
