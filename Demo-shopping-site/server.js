const express = require('express');
const cors = require('cors');
const path = require('path');
const dotenv = require('dotenv');

// Load environment variables
dotenv.config();

// Attempt to load Gateway SDK
let AgentCommerceGateway;
try {
  AgentCommerceGateway = require('../agent-commerce-gateway/sdk/agent_commerce_gateway.js');
} catch (err) {
  console.warn('Warning: Could not load AgentCommerceGateway SDK. Checkout intent will fail.');
}

const app = express();
app.use(cors());
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

// --- Runtime Config (in-memory merchant credentials) ---
let runtimeConfig = {
  apiKey: process.env.MERCHANT_API_KEY || null,
  gatewayUrl: process.env.GATEWAY_URL || 'https://ai-agent-commerce-gateway-cjpp.vercel.app',
  merchantId: null,
  connected: false
};

// --- Mock Data ---
const products = [
  {
    id: "prod_shoe_001",
    name: "AeroGlide Runner",
    price: 8500,
    currency: "INR",
    category: "Running",
    description: "Ultra-lightweight running shoe with maximum energy return and breathable mesh upper.",
    stock: 45,
    sizes: ["7", "8", "9", "10", "11"],
    availability: "in_stock",
    image: "https://images.unsplash.com/photo-1542291026-7eec264c27ff?w=800&auto=format&fit=crop&q=80"
  },
  {
    id: "prod_shoe_002",
    name: "Urban Kicks Classic",
    price: 5200,
    currency: "INR",
    category: "Casual",
    description: "Everyday casual sneaker with vintage aesthetics and modern comfort.",
    stock: 120,
    sizes: ["6", "7", "8", "9", "10"],
    availability: "in_stock",
    image: "https://images.unsplash.com/photo-1600185365483-26d7a4cc7519?w=800&auto=format&fit=crop&q=80"
  },
  {
    id: "prod_shoe_003",
    name: "Trail Blazer GTX",
    price: 11000,
    currency: "INR",
    category: "Outdoor",
    description: "Waterproof hiking shoe with aggressive traction for all terrains.",
    stock: 12,
    sizes: ["8", "9", "10", "11", "12"],
    availability: "low_stock",
    image: "https://images.unsplash.com/photo-1551107696-a4b0c5a0d9a2?w=800&auto=format&fit=crop&q=80"
  },
  {
    id: "prod_shoe_004",
    name: "Court Master Pro",
    price: 7800,
    currency: "INR",
    category: "Basketball",
    description: "High-top basketball shoe providing superior ankle support and grip.",
    stock: 30,
    sizes: ["9", "10", "11", "12"],
    availability: "in_stock",
    image: "https://images.unsplash.com/photo-1579338559194-a162d19bf842?w=800&auto=format&fit=crop&q=80"
  },
  {
    id: "prod_shoe_005",
    name: "Slip-On Comfort",
    price: 3500,
    currency: "INR",
    category: "Casual",
    description: "Easy slip-on design with memory foam insole for all-day comfort.",
    stock: 200,
    sizes: ["5", "6", "7", "8", "9", "10"],
    availability: "in_stock",
    image: "https://images.unsplash.com/photo-1560769629-975ec94e6a86?w=800&auto=format&fit=crop&q=80"
  },
  {
    id: "prod_shoe_006",
    name: "Sprint Spike 300",
    price: 9500,
    currency: "INR",
    category: "Running",
    description: "Track spike designed for sprinters seeking maximum speed and minimal weight.",
    stock: 0,
    sizes: ["7", "8", "9"],
    availability: "out_of_stock",
    image: "https://images.unsplash.com/photo-1584735935682-2f2b69dff9d2?w=800&auto=format&fit=crop&q=80"
  },
  {
    id: "prod_shoe_007",
    name: "Leather Oxford Elite",
    price: 14500,
    currency: "INR",
    category: "Formal",
    description: "Premium handcrafted leather oxford shoe for formal occasions.",
    stock: 25,
    sizes: ["8", "9", "10", "11"],
    availability: "in_stock",
    image: "https://images.unsplash.com/photo-1614252235316-8c857d38b5f4?w=800&auto=format&fit=crop&q=80"
  },
  {
    id: "prod_shoe_008",
    name: "Aqua Walker",
    price: 4200,
    currency: "INR",
    category: "Outdoor",
    description: "Quick-drying water shoe perfect for beach and river activities.",
    stock: 85,
    sizes: ["6", "7", "8", "9", "10", "11"],
    availability: "in_stock",
    image: "https://images.unsplash.com/photo-1595950653106-6c9ebd614d3a?w=800&auto=format&fit=crop&q=80"
  },
  {
    id: "prod_shoe_009",
    name: "Velocity Nitro",
    price: 10200,
    currency: "INR",
    category: "Running",
    description: "Advanced running shoe with nitrogen-infused foam for explosive speed.",
    stock: 50,
    sizes: ["7", "8", "9", "10", "11"],
    availability: "in_stock",
    image: "https://images.unsplash.com/photo-1608231387042-66d1773070a5?w=800&auto=format&fit=crop&q=80"
  },
  {
    id: "prod_shoe_010",
    name: "Suede Loafer",
    price: 6800,
    currency: "INR",
    category: "Casual",
    description: "Classic suede loafer, effortlessly stylish and comfortable.",
    stock: 40,
    sizes: ["7", "8", "9", "10"],
    availability: "in_stock",
    image: "https://images.unsplash.com/photo-1533867617858-e7b97e060509?w=800&auto=format&fit=crop&q=80"
  }
];

// Simple in-memory cart (for demo purposes)
let cart = {
  items: [],
  total_amount: 0,
  currency: "INR"
};

// --- Helper to update cart total ---
function updateCartTotal() {
  cart.total_amount = cart.items.reduce((total, item) => {
    return total + (item.price * item.quantity);
  }, 0);
}

// --- APIs ---

// 0. GET /pay (Merchant Connection Page)
app.get('/pay', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'pay.html'));
});

// 0.05 GET /api/dashboard-redirect
app.get('/api/dashboard-redirect', (req, res) => {
  const targetUrl = (runtimeConfig.gatewayUrl || process.env.GATEWAY_URL || 'https://ai-agent-commerce-gateway-cjpp.vercel.app').replace(/\/$/, '');
  res.redirect(`${targetUrl}/api/dashboard/`);
});

// 0.1 GET /api/connection-status
app.get('/api/connection-status', (req, res) => {
  res.json({
    connected: runtimeConfig.connected,
    merchantId: runtimeConfig.merchantId ? runtimeConfig.merchantId : (runtimeConfig.apiKey ? 'Key: ' + runtimeConfig.apiKey.substring(0, 10) + '...' : null),
    gatewayUrl: runtimeConfig.gatewayUrl
  });
});

// 0.2 POST /api/connect
app.post('/api/connect', async (req, res) => {
  const { apiKey, gatewayUrl } = req.body;
  if (!apiKey) {
    return res.status(400).json({ error: 'API key is required' });
  }
  const cleanApiKey = String(apiKey).trim().replace(/[^\x20-\x7E]/g, "");
  const targetGatewayUrl = (gatewayUrl || runtimeConfig.gatewayUrl || 'https://ai-agent-commerce-gateway-cjpp.vercel.app').replace(/\/$/, "");

  if (!AgentCommerceGateway) {
    return res.status(500).json({ error: 'Gateway SDK not available on server' });
  }

  try {
    const gateway = new AgentCommerceGateway(cleanApiKey, targetGatewayUrl);
    const verifyResult = await gateway.verifyCredentials();

    runtimeConfig.apiKey = cleanApiKey;
    runtimeConfig.gatewayUrl = targetGatewayUrl;
    runtimeConfig.merchantId = verifyResult.merchant_id || 'merchant-demo-001';
    runtimeConfig.connected = true;

    res.json({ 
      success: true, 
      message: 'Connected successfully to Agent Commerce Gateway',
      merchant_id: runtimeConfig.merchantId 
    });
  } catch (err) {
    console.error('Connection failed:', err);
    res.status(err.status || 401).json({ 
      error: err.message || 'Connection failed. Please check your API key and Gateway URL.' 
    });
  }
});

// 0.3 POST /api/disconnect
app.post('/api/disconnect', (req, res) => {
  runtimeConfig.apiKey = null;
  runtimeConfig.gatewayUrl = 'https://ai-agent-commerce-gateway-cjpp.vercel.app';
  runtimeConfig.merchantId = null;
  runtimeConfig.connected = false;
  res.json({ success: true });
});

// 1. GET /api/products (Catalog API)
app.get('/api/products', (req, res) => {
  const { search, category } = req.query;
  let results = [...products];

  if (category) {
    results = results.filter(p => p.category.toLowerCase() === category.toLowerCase());
  }

  if (search) {
    const s = search.toLowerCase();
    results = results.filter(p => 
      p.name.toLowerCase().includes(s) || 
      p.description.toLowerCase().includes(s)
    );
  }

  res.json({
    count: results.length,
    data: results
  });
});

// 2. GET /api/products/:id (Product lookup API)
app.get('/api/products/:id', (req, res) => {
  const product = products.find(p => p.id === req.params.id);
  if (!product) {
    return res.status(404).json({ error: 'Product not found' });
  }
  res.json({ data: product });
});

// 3. GET /api/cart (Fetch current cart)
app.get('/api/cart', (req, res) => {
  updateCartTotal();
  res.json({ data: cart });
});

// 3.1 POST /api/cart (Cart operations)
app.post('/api/cart', (req, res) => {
  const { id, quantity, size } = req.body;
  
  if (!id || typeof quantity !== 'number') {
    return res.status(400).json({ error: 'Missing or invalid id or quantity' });
  }

  const product = products.find(p => p.id === id);
  if (!product) {
    return res.status(404).json({ error: 'Product not found' });
  }

  if (quantity <= 0) {
    // Remove from cart
    cart.items = cart.items.filter(item => item.id !== id);
  } else {
    // Check stock
    if (quantity > product.stock) {
      return res.status(400).json({ error: 'Not enough stock available' });
    }

    const existingItem = cart.items.find(item => item.id === id);
    if (existingItem) {
      existingItem.quantity = quantity;
      if (size) existingItem.size = size;
    } else {
      cart.items.push({
        id: product.id,
        name: product.name,
        price: product.price,
        quantity: quantity,
        size: size || product.sizes[0],
        category: product.category
      });
    }
  }

  updateCartTotal();
  res.json({ data: cart });
});

// 3.2 DELETE /api/cart (Clear cart)
app.delete('/api/cart', (req, res) => {
  cart.items = [];
  updateCartTotal();
  res.json({ success: true, data: cart });
});

// 4. POST /api/checkout-intent (Checkout intent via ACP)
app.post('/api/checkout-intent', async (req, res) => {
  if (cart.items.length === 0) {
    return res.status(400).json({ error: 'Cart is empty' });
  }

  let merchantApiKey = runtimeConfig.apiKey || process.env.MERCHANT_API_KEY || 'sk_test_f22ff116facae2ec5d6a6266cb366dae0e93d85674311019';
  if (merchantApiKey) {
    merchantApiKey = merchantApiKey.replace(/['"]/g, '').trim(); // Remove any accidental quotes from Vercel env
  }
  
  if (!merchantApiKey) {
    return res.status(400).json({ 
      error: 'Storefront is not connected to Agent Commerce Gateway. Please visit /pay to connect your API key.' 
    });
  }

  if (!AgentCommerceGateway) {
    return res.status(500).json({ error: 'Gateway SDK not available' });
  }

  try {
    const gatewayUrl = runtimeConfig.gatewayUrl || process.env.GATEWAY_URL || 'https://ai-agent-commerce-gateway-cjpp.vercel.app';
    const gateway = new AgentCommerceGateway(merchantApiKey, gatewayUrl);
    
    // Convert cart items to ACP line items in minor currency units (paise)
    const acpItems = cart.items.map(item => ({
      id: item.id,
      name: item.name,
      quantity: item.quantity,
      unit_amount: Math.round(item.price * 100),
      currency: (cart.currency || 'INR').toUpperCase(),
      category: item.category || 'Footwear'
    }));

    const idempotencyKey = `acp_${Date.now()}_${Math.random().toString(36).substring(2, 8)}`;
    const merchantId = runtimeConfig.merchantId || 'merchant-demo-001';

    // Forward incoming raw protocol payload from AI Buyer if provided, otherwise convert local cart
    const protocol = (req.body && req.body.protocol) ? req.body.protocol : 'acp';
    const payload = (req.body && req.body.payload) ? req.body.payload : {
      items: acpItems,
      buyer: {
        agent_id: "agent_shopper_001",
        first_name: "Demo",
        last_name: "Buyer",
        email: "shopper@agentcommerce.ai"
      },
      merchant_id: merchantId,
      idempotency_key: idempotencyKey,
      bearer_token: `acp_token_${Date.now()}`,
      api_version: "2026-01-16"
    };

    // Execute protocol request through the gateway (POST /api/v1/execute)
    let result;
    try {
      result = await gateway.executeRequest(protocol, payload);
    } catch (apiErr) {
      if (apiErr.status === 401 && runtimeConfig.apiKey) {
        console.warn('Gateway returned 401. Vercel likely cold-started and lost in-memory state. Retrying with default key.');
        const defaultKey = 'sk_test_f22ff116facae2ec5d6a6266cb366dae0e93d85674311019';
        runtimeConfig.apiKey = defaultKey; // Reset for future requests
        gateway.apiKey = defaultKey;
        result = await gateway.executeRequest(protocol, payload);
      } else {
        throw apiErr;
      }
    }

    if (result.gateway_decision === 'ALLOW') {
      const currentTotal = cart.total_amount;
      const currentCurrency = cart.currency;
      
      // Clear cart after intent succeeds
      cart.items = [];
      updateCartTotal();

      res.json({
        success: true,
        data: {
          ...result,
          intent_status: 'created',
          total_amount: currentTotal,
          currency: currentCurrency,
          raw_acp_payload: payload // for backwards compatibility
        }
      });
    } else {
      res.status(400).json({
        success: false,
        error: `Gateway Decision: ${result.gateway_decision || 'BLOCK'}`,
        details: result.reason || 'Transaction rejected by Gateway policy',
        data: {
          ...result,
          razorpay_order_id: null,
          razorpay_key_id: null,
          raw_acp_payload: payload // for backwards compatibility
        }
      });
    }
  } catch (err) {
    console.error('Gateway Error:', err);
    res.status(err.status || 500).json({ 
      error: 'Gateway checkout intent failed', 
      details: err.message,
      gatewayResponse: err.gatewayResponse
    });
  }
});

// 5. POST /api/verify-payment (Verify client payment & capture via Gateway)
app.post('/api/verify-payment', async (req, res) => {
  const { razorpay_order_id, razorpay_payment_id, razorpay_signature, amount_minor, currency } = req.body;
  if (!razorpay_order_id || !razorpay_payment_id || !razorpay_signature) {
    return res.status(400).json({ error: 'Missing required payment callback parameters' });
  }

  let merchantApiKey = runtimeConfig.apiKey || process.env.MERCHANT_API_KEY || 'sk_test_f22ff116facae2ec5d6a6266cb366dae0e93d85674311019';
  if (merchantApiKey) {
    merchantApiKey = merchantApiKey.replace(/['"]/g, '').trim();
  }
  if (!merchantApiKey) {
    return res.status(400).json({ error: 'Storefront is not connected to Agent Commerce Gateway' });
  }

  try {
    const gatewayUrl = runtimeConfig.gatewayUrl || process.env.GATEWAY_URL || 'https://ai-agent-commerce-gateway-cjpp.vercel.app';
    const gateway = new AgentCommerceGateway(merchantApiKey, gatewayUrl);

    const result = await gateway.verifyPayment({
      razorpay_order_id,
      razorpay_payment_id,
      razorpay_signature,
      amount_minor: amount_minor || 0,
      currency: currency || 'INR'
    });

    res.json({
      success: result.success,
      data: result
    });
  } catch (err) {
    console.error('Payment verification error:', err);
    res.status(err.status || 500).json({
      error: 'Payment verification failed',
      details: err.message
    });
  }
});

// For testing purposes: reset cart endpoint
app.post('/api/test/reset-cart', (req, res) => {
  cart.items = [];
  updateCartTotal();
  res.json({ success: true });
});

// For testing purposes: set runtime config
app.post('/api/test/set-config', (req, res) => {
  runtimeConfig = { ...runtimeConfig, ...req.body };
  res.json({ success: true, config: runtimeConfig });
});

const PORT = process.env.PORT || 3000;
if (require.main === module && !process.env.VERCEL) {
  app.listen(PORT, () => {
    console.log(`Demo Merchant Storefront running on port ${PORT}`);
  });
}

module.exports = app; // export for testing
