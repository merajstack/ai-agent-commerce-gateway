const express = require('express');
const cors = require('cors');
const path = require('path');
const config = require('./src/config');
const gemini = require('./src/gemini');
const toolHandlers = require('./src/tools/handlers');
const { listProtocols, generateProtocolPayload } = require('./src/protocols');

const app = express();

app.use(cors());
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

// 1. Status & Info API
app.get('/api/status', async (req, res) => {
  let storefrontConnected = false;
  let cart = { items: [], total_amount: 0, currency: 'INR' };
  
  try {
    const storeRes = await fetch(`${config.STOREFRONT_URL}/api/products`);
    storefrontConnected = storeRes.ok;
    cart = await toolHandlers.getCart();
  } catch (e) {
    storefrontConnected = false;
  }

  res.json({
    name: 'AI Buyer',
    version: '1.0.0',
    gemini_configured: Boolean(config.GEMINI_API_KEY && config.GEMINI_API_KEY.trim().length > 5),
    gemini_model: config.GEMINI_MODEL,
    storefront_url: config.STOREFRONT_URL,
    storefront_connected: storefrontConnected,
    default_protocol: 'acp',
    protocols: listProtocols(),
    cart: cart
  });
});

// 2. Protocol List API
app.get('/api/protocols', (req, res) => {
  res.json({
    protocols: listProtocols()
  });
});

// 3. Cart Proxy API
app.get('/api/cart', async (req, res) => {
  const cart = await toolHandlers.getCart();
  res.json({ data: cart });
});

app.post('/api/cart', async (req, res) => {
  try {
    const { id, quantity, size } = req.body;
    if (typeof quantity === 'number' && quantity <= 0) {
      // Clear or remove item
      try {
        await fetch(`${toolHandlers.storefrontUrl}/api/cart`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id, quantity: 0 })
        });
      } catch (e) {}
      toolHandlers.cart.items = toolHandlers.cart.items.filter(i => i.id !== id);
      toolHandlers.cart.total_amount = toolHandlers.cart.items.reduce((s, i) => s + i.price * i.quantity, 0);
      return res.json({ data: toolHandlers.cart });
    }
    const result = await toolHandlers.addToCart({ id, quantity, size });
    res.json({ data: result.cart });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.delete('/api/cart', async (req, res) => {
  try {
    toolHandlers.cart = { items: [], total_amount: 0, currency: 'INR' };
    try {
      await fetch(`${toolHandlers.storefrontUrl}/api/cart`, { method: 'DELETE' });
    } catch (e) {}
    res.json({ success: true, data: toolHandlers.cart });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// 4. Raw Protocol Generator API (inspect without checkout)
app.post('/api/generate-protocol', async (req, res) => {
  try {
    const { protocol = 'acp' } = req.body;
    const cart = await toolHandlers.getCart();
    const result = generateProtocolPayload(protocol, cart, {
      buyerIdentity: config.BUYER_IDENTITY,
      merchantId: 'merchant-demo-001'
    });
    res.json({ success: true, data: result });
  } catch (err) {
    res.status(400).json({ success: false, error: err.message });
  }
});

// 5. Main Chat & Agent Execution Endpoint
app.post('/api/chat', async (req, res) => {
  try {
    const { message, history = [], protocol = 'acp' } = req.body;
    if (!message || typeof message !== 'string') {
      return res.status(400).json({ error: 'Message string is required' });
    }

    const result = await gemini.processMessage(message, history, protocol);
    
    // Also fetch fresh cart for UI sync
    const updatedCart = await toolHandlers.getCart();

    res.json({
      success: true,
      data: {
        message: result.message,
        activityLogs: result.activityLogs,
        rawProtocolRequest: result.rawProtocolRequest,
        gatewayResponse: result.gatewayResponse,
        protocolMetadata: result.protocolMetadata,
        selectedProtocol: protocol,
        cart: updatedCart
      }
    });
  } catch (err) {
    console.error('Chat error:', err);
    res.status(500).json({
      success: false,
      error: 'Agent processing error',
      details: err.message
    });
  }
});

// 6. Payment Verification Endpoint (relays to Storefront / Gateway POST /api/v1/payments/verify)
app.post('/api/verify-payment', async (req, res) => {
  try {
    const { razorpay_order_id, razorpay_payment_id, razorpay_signature, amount_minor, currency } = req.body;
    if (!razorpay_order_id || !razorpay_payment_id || !razorpay_signature) {
      return res.status(400).json({
        success: false,
        error: 'Missing required payment verification parameters (order_id, payment_id, signature)'
      });
    }

    const result = await toolHandlers.verifyPayment({
      razorpay_order_id,
      razorpay_payment_id,
      razorpay_signature,
      amount_minor: Number(amount_minor) || 0,
      currency: currency || 'INR'
    });

    res.json(result);
  } catch (err) {
    console.error('Payment verification route error:', err);
    res.status(500).json({
      success: false,
      error: 'Payment verification failed',
      details: err.message
    });
  }
});

const PORT = config.PORT || 4000;
if (process.env.NODE_ENV !== 'test' && !process.env.VERCEL) {
  app.listen(PORT, () => {
    console.log(`🤖 AI Buyer Agent running on port ${PORT}`);
    console.log(`📡 Connected Storefront: ${config.STOREFRONT_URL}`);
    console.log(`🔑 Gemini Model: ${config.GEMINI_MODEL}`);
  });
}

module.exports = app;
