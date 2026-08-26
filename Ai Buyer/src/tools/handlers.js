const config = require('../config');
const { generateProtocolPayload, validateX402Payload } = require('../protocols');

class ToolHandlers {
  constructor(storefrontUrl = config.STOREFRONT_URL) {
    this.storefrontUrl = storefrontUrl;
  }

  async searchProducts({ query = '', category = '' } = {}) {
    try {
      const url = new URL(`${this.storefrontUrl}/api/products`);
      const genericWords = ['best', 'top', 'shoe', 'shoes', 'recommend', 'recommendation', 'good', 'nice', 'pair', 'something', 'one', '1'];
      const cleaned = (query || '').trim();
      const isGeneric = genericWords.includes(cleaned.toLowerCase());

      if (cleaned && !isGeneric) {
        url.searchParams.append('search', cleaned);
      }
      if (category) url.searchParams.append('category', category);

      let res = await fetch(url.toString());
      if (!res.ok) {
        throw new Error(`Store catalog API returned HTTP ${res.status}`);
      }
      let data = await res.json();

      // If specific search returned empty, fallback to full catalog or category
      if ((!data.data || data.data.length === 0) && cleaned) {
        const fallbackUrl = new URL(`${this.storefrontUrl}/api/products`);
        if (category) fallbackUrl.searchParams.append('category', category);
        const fbRes = await fetch(fallbackUrl.toString());
        if (fbRes.ok) {
          data = await fbRes.json();
        }
      }

      return {
        success: true,
        count: data.count,
        products: data.data
      };
    } catch (err) {
      return {
        success: false,
        error: `Failed to search products: ${err.message}`
      };
    }
  }

  async getProduct({ id }) {
    try {
      let targetId = id;
      if (id && !id.startsWith('prod_')) {
        const search = await this.searchProducts({ query: id });
        if (search.success && search.products.length > 0) {
          targetId = search.products[0].id;
        }
      }

      const res = await fetch(`${this.storefrontUrl}/api/products/${targetId}`);
      if (!res.ok) {
        throw new Error(`Product ${id} not found`);
      }
      const data = await res.json();
      return {
        success: true,
        product: data.data
      };
    } catch (err) {
      return {
        success: false,
        error: `Failed to get product ${id}: ${err.message}`
      };
    }
  }

  async getCart() {
    try {
      const res = await fetch(`${this.storefrontUrl}/api/cart`);
      if (!res.ok) {
        throw new Error(`Failed to fetch cart: HTTP ${res.status}`);
      }
      const data = await res.json();
      return data.data || { items: [], total_amount: 0, currency: 'INR' };
    } catch (err) {
      return { items: [], total_amount: 0, currency: 'INR' };
    }
  }

  async addToCart({ id, name, quantity = 1, size }) {
    try {
      let resolvedId = id;
      let matchedProduct = null;

      const search = await this.searchProducts();
      if (search.success && search.products) {
        if (id && id.startsWith('prod_')) {
          matchedProduct = search.products.find(p => p.id === id);
        }
        if (!matchedProduct && (id || name)) {
          const searchTerm = (id || name).toLowerCase().trim();
          matchedProduct = search.products.find(p => p.name.toLowerCase() === searchTerm) ||
                           search.products.find(p => p.name.toLowerCase().includes(searchTerm)) ||
                           search.products.find(p => searchTerm.includes(p.name.toLowerCase()));
        }
      }

      if (matchedProduct) {
        resolvedId = matchedProduct.id;
      } else if (!resolvedId) {
        resolvedId = 'prod_shoe_001';
      }

      const chosenSize = size ? String(size) : (matchedProduct && matchedProduct.sizes ? matchedProduct.sizes[0] : '9');
      const numQty = parseInt(quantity, 10) || 1;

      const res = await fetch(`${this.storefrontUrl}/api/cart`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: resolvedId, quantity: numQty, size: chosenSize })
      });

      const data = await res.json();
      if (!res.ok) {
        return {
          success: false,
          error: data.error || 'Failed to add item to cart'
        };
      }

      return {
        success: true,
        item_added: matchedProduct ? matchedProduct.name : resolvedId,
        quantity: numQty,
        size: chosenSize,
        cart: data.data
      };
    } catch (err) {
      return {
        success: false,
        error: `Network error adding to cart: ${err.message}`
      };
    }
  }

  async updateCart({ id, quantity }) {
    return this.addToCart({ id, quantity: parseInt(quantity, 10) || 0 });
  }

  async executeCheckout(protocolId = 'acp') {
    try {
      const normalizedId = (protocolId || 'acp').toLowerCase();

      // 1. Check for unsupported protocols (AP2)
      if (normalizedId === 'ap2') {
        return {
          success: false,
          is_protocol_request: true,
          protocol: 'ap2',
          protocol_name: 'AP2 (Agent Payment Protocol)',
          protocol_version: 'v1.0.0-draft',
          raw_protocol_payload: null,
          status_notice: 'PROTOCOL_UNSUPPORTED',
          error: 'AP2 is currently unsupported (Coming next). ACP (2026-01-16) and x402 v2 are the active protocols.',
          details: 'Mandate-driven AP2 adapter is in progress.'
        };
      }

      // 2. Fetch current cart
      const cart = await this.getCart();
      if (!cart.items || cart.items.length === 0) {
        return {
          success: false,
          error: 'Cannot checkout: Cart is empty. Please add a product first.'
        };
      }

      // 3. Generate protocol payload
      const protocolInfo = generateProtocolPayload(normalizedId, cart, {
        buyerIdentity: config.BUYER_IDENTITY,
        merchantId: 'merchant-demo-001'
      });

      // Strict validation for x402
      if (normalizedId === 'x402') {
        validateX402Payload(protocolInfo.payload);
      }

      // Send AI-generated raw protocol request to Merchant Storefront -> Gateway POST /api/v1/execute
      const res = await fetch(`${this.storefrontUrl}/api/checkout-intent`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          protocol: normalizedId,
          payload: protocolInfo.payload
        })
      });

      const responseJson = await res.json();

      return {
        success: res.ok && (responseJson.success || responseJson.data?.gateway_decision === 'ALLOW'),
        is_protocol_request: true,
        protocol: normalizedId,
        protocol_name: protocolInfo.protocolName,
        protocol_version: protocolInfo.protocolVersion,
        raw_protocol_payload: protocolInfo.payload,
        gateway_data: responseJson.data || responseJson,
        error: res.ok ? null : (responseJson.error || responseJson.details || 'Checkout execution failed')
      };
    } catch (err) {
      return {
        success: false,
        error: `Checkout execution failed: ${err.message}`
      };
    }
  }

  async verifyPayment(paymentDetails) {
    try {
      const res = await fetch(`${this.storefrontUrl}/api/verify-payment`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(paymentDetails)
      });
      const data = await res.json();
      return {
        success: res.ok && data.success,
        data: data.data,
        error: res.ok ? null : (data.error || 'Payment verification failed')
      };
    } catch (err) {
      return {
        success: false,
        error: `Network error verifying payment: ${err.message}`
      };
    }
  }

  async executeTool(name, args = {}, protocolId = 'acp') {
    switch (name) {
      case 'search_products':
        return await this.searchProducts(args);
      case 'get_product':
        return await this.getProduct(args);
      case 'get_cart':
        return await this.getCart();
      case 'add_to_cart':
        return await this.addToCart(args);
      case 'update_cart':
        return await this.updateCart(args);
      case 'checkout':
        return await this.executeCheckout(protocolId);
      default:
        return { error: `Unknown tool: ${name}` };
    }
  }
}

module.exports = new ToolHandlers();
