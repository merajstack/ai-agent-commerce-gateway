const config = require('./config');
const toolDefinitions = require('./tools/definitions');
const toolHandlers = require('./tools/handlers');
const { generateProtocolPayload } = require('./protocols');

/**
 * AI Shopping Agent Engine
 * Features:
 * - Stateful multi-turn conversation tracking
 * - Gemini 3.5 Flash with Function Calling
 * - Autonomous Deterministic Planner for offline / fallback execution
 * - Strict separation of:
 *     1. Casual Conversation / Greetings (Friendly conversational reply only)
 *     2. Recommendation / Advice (Curated 1-2 shoe picks with rationale, NOT raw dumps)
 *     3. Decision Delegation ("choose 1" / "pick one" selects the best shoe and prompts for buy)
 *     4. Search / Browse (Filtered catalog view, no protocol request)
 *     5. Price Quotes & Calculations (Informational only, no checkout)
 *     6. Transaction / Purchase Intent (ONLY state that generates ACP/x402 protocol requests)
 */
class AIAgentEngine {
  constructor() {
    this.models = ['gemini-3.5-flash', 'gemini-3.5-flash-lite'];
    this.systemInstruction = `You are an intelligent, friendly, and conversational AI Shopping Assistant powered by the Agent Commerce Gateway.
You shop like an expert stylist and human shopping companion: you understand nuances, exact product names, quantities, shoe sizes, budgets, and multi-turn context.

FUNDAMENTAL PRINCIPLE:
Conversation ≠ protocol request.
Purchase intent → protocol request.

CONVERSATIONAL RULES:
1. GREETINGS & CASUAL CHAT:
   - If the user sends a greeting (e.g. "hey", "hello", "hi", "how are you", "what's up"):
     → Answer naturally and warmly: "I’m doing well! What would you like to shop for?"
     → DO NOT search the catalog, DO NOT list product recommendations, and DO NOT initiate any protocol flow.
2. RECOMMENDATION REQUESTS ("recommend me", "what's the best option", "what should I buy"):
   - Recommend only 1 or 2 specific top items with concise justifications (e.g. AeroGlide Runner for running performance, Urban Kicks for everyday casual).
   - DO NOT dump the full 10-product catalog. Keep recommendations focused.
3. ADVICE & DECISION DELEGATION ("choose 1", "pick one for me", "which one is better"):
   - Pick the single best match (default to AeroGlide Runner in Size 9 if unspecified).
   - Explain why it was selected and ask the user if they'd like to proceed with checkout.
   - DO NOT execute checkout automatically on delegation; wait for purchase intent.
4. SEARCH & BROWSE ("find running shoes under ₹5,000", "show sneakers in size 9"):
   - Present a neat, relevant list matching the user's criteria.
   - DO NOT generate protocol requests or initiate checkout.
5. PRICE QUOTES & CART INQUIRIES ("how much for 3 AeroGlide", "total for 2 pairs"):
   - Calculate and state the accurate total (unit price × quantity).
   - Ask for confirmation before initiating purchase.
6. TRANSACTION & CHECKOUT INTENT ("buy the shoes", "checkout", "okay buy them", "purchase now"):
   - Add the specified item, quantity, and size to the storefront cart.
   - Generate the protocol request and submit to the Agent Commerce Gateway.
   - Present the authorization result and payment handoff.

Always maintain conversational coherence and remember the product discussed in previous turns.`;
  }

  /**
   * Main chat turn handler
   * @param {string} userMessage - User's prompt
   * @param {Array} history - Previous messages
   * @param {string} protocolId - Selected protocol ('acp', 'x402', 'ap2')
   */
  async processMessage(userMessage, history = [], protocolId = 'acp') {
    const activityLogs = [];
    const activeApiKey = config.GEMINI_API_KEY;

    // 1. Try Gemini Function-Calling Loop if API key exists
    if (activeApiKey) {
      for (const modelName of this.models) {
        try {
          const geminiResult = await this._callGeminiWithTools(userMessage, history, protocolId, activityLogs, activeApiKey, modelName);
          if (geminiResult && geminiResult.message) {
            return geminiResult;
          }
        } catch (err) {
          if (err.message && (err.message.includes('429') || err.message.includes('503') || err.message.includes('RESOURCE_EXHAUSTED') || err.name === 'TimeoutError')) {
            console.warn(`[AI Buyer] Gemini API busy (${err.message.slice(0, 50)}...), switching to autonomous planner immediately.`);
            break; // Stop looping to avoid user-perceived latency
          }
          console.warn(`[AI Buyer] Gemini attempt with ${modelName} failed: ${err.message}`);
        }
      }
    }

    // 2. Fallback to Autonomous Stateful Planner
    return await this._runDeterministicPlanner(userMessage, history, protocolId, activityLogs);
  }

  async _callGeminiWithTools(userMessage, history, protocolId, activityLogs, apiKey, modelName) {
    const endpoint = `https://generativelanguage.googleapis.com/v1beta/models/${modelName}:generateContent?key=${apiKey}`;
    
    // Prepare contents
    const contents = [];
    
    // Convert history (up to last 10 turns)
    if (history && history.length > 0) {
      for (const msg of history.slice(-10)) {
        contents.push({
          role: msg.role === 'assistant' ? 'model' : 'user',
          parts: [{ text: msg.content }]
        });
      }
    }
    
    // Current user message
    contents.push({
      role: 'user',
      parts: [{ text: userMessage }]
    });

    let currentTurn = 0;
    const maxTurns = 6;
    let finalAssistantText = '';
    let rawProtocolRequest = null;
    let gatewayResponse = null;
    let protocolMetadata = null;

    while (currentTurn < maxTurns) {
      currentTurn++;

      const requestBody = {
        contents: contents,
        systemInstruction: {
          parts: [{ text: this.systemInstruction }]
        },
        tools: toolDefinitions,
        generationConfig: {
          temperature: 0.2,
          maxOutputTokens: 1024
        }
      };

      const response = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(requestBody),
        signal: AbortSignal.timeout(4000)
      });

      if (!response.ok) {
        const errText = await response.text();
        throw new Error(`Gemini API Error ${response.status}: ${errText}`);
      }

      const data = await response.json();
      const candidate = data.candidates?.[0];
      if (!candidate || !candidate.content) {
        throw new Error('No content returned from Gemini');
      }

      const parts = candidate.content.parts || [];
      const functionCalls = parts.filter(p => p.functionCall);
      const textParts = parts.filter(p => p.text).map(p => p.text);

      if (textParts.length > 0) {
        finalAssistantText += (finalAssistantText ? '\n\n' : '') + textParts.join('\n');
      }

      // If no tool calls, model is finished
      if (functionCalls.length === 0) {
        break;
      }

      // Append model turn with function calls
      contents.push(candidate.content);

      // Execute each function call
      const functionResponses = [];
      for (const fc of functionCalls) {
        const toolName = fc.functionCall.name;
        const toolArgs = fc.functionCall.args || {};

        activityLogs.push({
          tool: toolName,
          status: 'running',
          args: toolArgs,
          message: `Executing ${toolName}...`
        });

        let toolResult;
        try {
          toolResult = await toolHandlers.executeTool(toolName, toolArgs, protocolId);
          activityLogs[activityLogs.length - 1].status = toolResult.error ? 'failed' : 'success';
          activityLogs[activityLogs.length - 1].result = toolResult;

          if (toolResult.raw_protocol_payload) {
            rawProtocolRequest = toolResult.raw_protocol_payload;
            protocolMetadata = {
              id: toolResult.protocol,
              name: toolResult.protocol_name,
              version: toolResult.protocol_version,
              status_notice: toolResult.status_notice
            };
          }
          if (toolResult.gateway_data) {
            gatewayResponse = toolResult.gateway_data;
          }
        } catch (err) {
          toolResult = { error: err.message };
          activityLogs[activityLogs.length - 1].status = 'failed';
          activityLogs[activityLogs.length - 1].error = err.message;
        }

        const fr = {
          name: toolName,
          response: { result: toolResult }
        };
        if (fc.functionCall.id) {
          fr.id = fc.functionCall.id;
        }
        functionResponses.push({
          functionResponse: fr
        });
      }

      // Feed function responses back to Gemini
      contents.push({
        role: 'user',
        parts: functionResponses
      });
    }

    const updatedCart = await toolHandlers.getCart();

    return {
      message: finalAssistantText,
      activityLogs,
      rawProtocolRequest,
      gatewayResponse,
      protocolMetadata,
      cart: updatedCart
    };
  }

  /**
   * Deterministic Autonomous Planner
   * Accurately parses user intent, extracts product/quantity/size, tracks multi-turn state,
   * and adheres strictly to:
   * 1. Greetings -> conversational answer only
   * 2. Recommendations -> curated 1-2 shoe suggestions with reasoning
   * 3. "Choose 1" / "Pick one" -> selects best shoe, persists state, asks confirmation
   * 4. Search / Browse -> filtered catalog list only (no raw dumps)
   * 5. Price query -> calculate total, no checkout
   * 6. Direct purchase / Confirmation -> execute checkout
   */
  async _runDeterministicPlanner(userMessage, history = [], protocolId = 'acp', activityLogs = []) {
    // Normalization for common typos & colloquial phrases
    let rawText = userMessage.toLowerCase().trim();
    let text = rawText
      .replace(/\bwhts\b|\bwat\b|\bwt\b/g, 'what')
      .replace(/\brecommendatin\b|\brecomended\b|\brecomendation\b|\brecommed\b|\brecommendations\b/g, 'recommendation')
      .replace(/\bchoode\b|\bchose\b|\bcoose\b/g, 'choose')
      .replace(/\bpls\b|\bplz\b/g, 'please');

    let responseText = '';
    let rawProtocolRequest = null;
    let gatewayResponse = null;
    let protocolMetadata = null;

    // ── 0. GREETINGS & CASUAL CONVERSATION ───────────────────────────────────
    const isGreeting = /^(hi|hello|hey|howdy|greetings|good\s+(?:morning|afternoon|evening)|yo|hey\s+there)\b/i.test(text) ||
                       /\b(how\s+are\s+you|who\s+are\s+you|what\s+can\s+you\s+do|what'?s\s+up|help)\b/i.test(text);
    const hasShoppingKeywords = /\b(shoe|shoes|sneaker|sneakers|runner|kicks|loafer|boots|buy|purchase|order|cart|checkout|price|cost|total|find|search|catalog|recommend|stock|size|under|below|₹|\$|choose|pick|select)\b/i.test(text);

    if (isGreeting && !hasShoppingKeywords) {
      return {
        message: "I’m doing well! What would you like to shop for?",
        activityLogs: [],
        rawProtocolRequest: null,
        gatewayResponse: null,
        protocolMetadata: null
      };
    }

    // Fetch catalog
    const catalogRes = await toolHandlers.searchProducts();
    let allProducts = catalogRes.products || [];
    if (!allProducts || allProducts.length === 0) {
      allProducts = [
        { id: 'prod_shoe_001', name: 'AeroGlide Runner', price: 8500, category: 'Running', stock: 45, sizes: ['7', '8', '9', '10', '11'] },
        { id: 'prod_shoe_002', name: 'Urban Kicks Classic', price: 5200, category: 'Casual', stock: 120, sizes: ['6', '7', '8', '9', '10'] },
        { id: 'prod_shoe_003', name: 'Trail Blazer GTX', price: 11000, category: 'Outdoor', stock: 12, sizes: ['8', '9', '10', '11', '12'] },
        { id: 'prod_shoe_004', name: 'Court Master Pro', price: 7800, category: 'Basketball', stock: 30, sizes: ['9', '10', '11', '12'] },
        { id: 'prod_shoe_005', name: 'Slip-On Comfort', price: 3500, category: 'Casual', stock: 200, sizes: ['5', '6', '7', '8', '9', '10'] },
        { id: 'prod_shoe_006', name: 'Sprint Spike 300', price: 9500, category: 'Running', stock: 0, sizes: ['7', '8', '9'] },
        { id: 'prod_shoe_007', name: 'Leather Oxford Elite', price: 14500, category: 'Formal', stock: 25, sizes: ['8', '9', '10', '11'] },
        { id: 'prod_shoe_008', name: 'Aqua Walker', price: 4200, category: 'Outdoor', stock: 85, sizes: ['6', '7', '8', '9', '10', '11'] },
        { id: 'prod_shoe_009', name: 'Velocity Nitro', price: 10200, category: 'Running', stock: 50, sizes: ['7', '8', '9', '10', '11'] },
        { id: 'prod_shoe_010', name: 'Suede Loafer', price: 6800, category: 'Casual', stock: 40, sizes: ['7', '8', '9', '10'] }
      ];
    }

    // Helper: Match product name in text
    const findProductInText = (str) => {
      if (!str) return null;
      const lower = str.toLowerCase();
      for (const p of allProducts) {
        if (lower.includes(p.name.toLowerCase())) return p;
      }
      for (const p of allProducts) {
        const parts = p.name.toLowerCase().split(' ');
        if (parts.some(part => part.length > 4 && lower.includes(part))) return p;
      }
      return null;
    };

    // Helper: Extract quantity (default 1)
    const extractQuantity = (str) => {
      if (!str) return 1;
      const wordMap = {
        'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
        'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
        'a pair of': 1, 'a pair': 1, '1 pair': 1, '2 pairs': 2, '3 pairs': 3
      };
      const lower = str.toLowerCase();
      for (const [w, n] of Object.entries(wordMap)) {
        if (lower.includes(w)) return n;
      }
      const cleaned = str.replace(/₹\s*\d+/g, '').replace(/rs\.?\s*\d+/gi, '').replace(/size\s*[:\s]?\s*\d+/gi, '');
      const explicitMatch = cleaned.match(/\b(\d+)\s*(?:pairs?|items?|shoes?|x\b|pcs?)/i) ||
                            cleaned.match(/(?:get|need|want|buy|order|for|take)\s+(\d+)\b/i) ||
                            cleaned.match(/\b(\d+)\s+[a-zA-Z]+/i);

      if (explicitMatch) {
        const val = parseInt(explicitMatch[1], 10);
        if (val >= 1 && val <= 50) return val;
      }
      return 1;
    };

    // Helper: Extract size
    const extractSize = (str) => {
      if (!str) return '9';
      const match = str.match(/size\s*[:\s]?\s*(\d+)/i);
      return match ? match[1] : '9';
    };

    // 1. RECONSTRUCT STATE FROM HISTORY AND CART
    let stateProduct = null;
    let stateQuantity = 1;
    let stateSize = '9';

    // Check storefront cart state
    const currentCart = await toolHandlers.getCart();
    if (currentCart.items && currentCart.items.length > 0) {
      const lastCartItem = currentCart.items[currentCart.items.length - 1];
      const matchInCatalog = allProducts.find(p => p.id === lastCartItem.id);
      if (matchInCatalog) {
        stateProduct = matchInCatalog;
        stateQuantity = lastCartItem.quantity || 1;
        stateSize = lastCartItem.size || '9';
      }
    }

    // Scan history backwards to recover discussed product/quantity/size
    if (history && history.length > 0) {
      for (let i = history.length - 1; i >= 0; i--) {
        const msgText = history[i].content;
        const foundProd = findProductInText(msgText);
        if (foundProd) {
          stateProduct = foundProd;
        }
        const q = extractQuantity(msgText);
        if (q > 1) {
          stateQuantity = q;
        }
        const s = extractSize(msgText);
        if (s && s !== '9') {
          stateSize = s;
        }
        if (stateProduct && stateQuantity > 1) break;
      }
    }

    // Check if current message has a new product, explicit quantity, or size
    const currentProductMatch = findProductInText(text);
    if (currentProductMatch) {
      stateProduct = currentProductMatch;
    }
    const hasExplicitQuantityInCurrentMsg = /(?:\b\d+\s*(?:pairs?|items?|shoes?|x\b|pcs?)|(?:get|need|want|buy|order|for|take)\s+\d+\b)/i.test(text);
    if (hasExplicitQuantityInCurrentMsg) {
      stateQuantity = extractQuantity(text);
    }
    if (text.includes('size')) {
      stateSize = extractSize(text);
    }

    // 2. CLASSIFY INTENT
    const pricePatterns = [
      'tell me the price', 'tell me the total', 'how much', 'what is the price',
      'what is the total', 'calculate total', 'price before', 'before buying',
      'cost of', 'check price', 'price for', 'total for', 'quote'
    ];
    const isPriceOnlyIntent = pricePatterns.some(p => text.includes(p));

    const isCartOnlyIntent = !isPriceOnlyIntent && (
      (text.includes('add to cart') || text.includes('put in cart') || text.includes('save to cart')) &&
      !text.includes('checkout') && !text.includes('buy')
    );

    const confirmPhrases = [
      'okay, buy', 'ok buy', 'buy them', 'buy it', 'buy these', 'buy those',
      'yes buy', 'yes, buy', 'yes please', 'proceed with purchase', 'proceed to checkout',
      'checkout now', 'confirm purchase', 'confirm order', 'place order', 'buy now',
      'and buy', 'buy the best', 'buy one', 'buy best', 'purchase them', 'order them'
    ];
    const isDirectBuy = (
      text.startsWith('buy ') || 
      text.startsWith('purchase ') || 
      text.startsWith('order ') ||
      text.includes('and buy') ||
      text.includes('buy the best') ||
      text.includes('buy best') ||
      text.includes('buy one')
    ) && !isPriceOnlyIntent;
    const isConfirmPhrase = confirmPhrases.some(p => text.includes(p));
    const isTransactionIntent = !isPriceOnlyIntent && (isConfirmPhrase || isDirectBuy);

    const isCartViewIntent = text.includes('cart') && (text.includes('show') || text.includes('view') || text.includes('what'));

    // Check for "Choose 1" / "Pick for me" / "You decide"
    const isChooseForMeIntent = /\b(choose\s*(?:one|1)?|pick\s*(?:one|1)?|select\s*(?:one|1)?|you\s+choose|you\s+pick|which\s+one\s+should\s+i|your\s+choice|decide\s+for\s+me)\b/i.test(text) && !isTransactionIntent;

    // Check for "Recommendation" / "Suggestion" / "What's best"
    const isRecommendationIntent = /\b(recommend|recommendation|suggest|suggestion|best\s+shoe|best\s+one|top\s+pick|favorite|favourite|what\s+do\s+you\s+suggest|what\s+should\s+i\s+buy)\b/i.test(text) && !isTransactionIntent && !isChooseForMeIntent;

    // 3. EXECUTE BASED ON STRICT INTENT

    // --- CASE A: View Cart ---
    if (isCartViewIntent) {
      if (!currentCart.items || currentCart.items.length === 0) {
        return {
          message: 'Your cart is currently empty. Would you like me to recommend some running shoes or casual sneakers for you?',
          activityLogs,
          rawProtocolRequest: null,
          gatewayResponse: null
        };
      }
      const itemsList = currentCart.items.map(i => `- **${i.name}** (Qty: ${i.quantity}, Size: ${i.size || '9'}) — ₹${(i.price * i.quantity).toLocaleString('en-IN')}`).join('\n');
      return {
        message: `### 🛒 Current Cart Items\n\n${itemsList}\n\n**Subtotal**: ₹${currentCart.total_amount.toLocaleString('en-IN')}\n\nReady to proceed? Just say **"Checkout with ${protocolId.toUpperCase()}"**!`,
        activityLogs,
        rawProtocolRequest: null,
        gatewayResponse: null
      };
    }

    // --- CASE B: Conversational Recommendation (1-2 Curated Picks) ---
    if (isRecommendationIntent) {
      activityLogs.push({
        tool: 'search_products',
        status: 'success',
        args: { query: 'recommendations' },
        message: 'Curating top recommendations from merchant catalog...'
      });

      const topRunning = allProducts.find(p => p.name.includes('AeroGlide')) || allProducts[0];
      const topCasual = allProducts.find(p => p.name.includes('Urban Kicks')) || allProducts[1];
      const topOutdoor = allProducts.find(p => p.name.includes('Aqua Walker')) || allProducts[7];

      responseText = `Here are my **top two picks** depending on what you're looking for:\n\n` +
        `1. 🏃 **${topRunning.name}** (₹${topRunning.price.toLocaleString('en-IN')}) — Our **#1 performance shoe**. Features ultra-lightweight responsive cushioning and breathable mesh for maximum energy return.\n` +
        `2. 👟 **${topCasual.name}** (₹${topCasual.price.toLocaleString('en-IN')}) — Perfect for **daily casual wear**. Vintage aesthetics with premium modern comfort.\n\n` +
        `💡 *Looking for great value under ₹5,000?* The **${topOutdoor.name}** (₹${topOutdoor.price.toLocaleString('en-IN')}) is also fantastic for outdoor versatility.\n\n` +
        `Which one catches your eye? You can say **"Choose 1 for me"**, **"Tell me the price for 2"**, or **"Buy the ${topRunning.name}"**!`;

      return {
        message: responseText,
        activityLogs,
        rawProtocolRequest: null,
        gatewayResponse: null,
        protocolMetadata: null
      };
    }

    // --- CASE C: "Choose 1 for me" Delegation ---
    if (isChooseForMeIntent) {
      const topChoice = stateProduct || allProducts.find(p => p.name.includes('AeroGlide')) || allProducts[0];
      stateProduct = topChoice;

      activityLogs.push({
        tool: 'get_product',
        status: 'success',
        args: { id: topChoice.id },
        result: { selected: topChoice.name, price: topChoice.price }
      });

      responseText = `If I had to pick just one, I definitely recommend the **${topChoice.name}** (₹${topChoice.price.toLocaleString('en-IN')}, Size ${stateSize})! 🏆\n\n` +
        `It offers the best overall balance of comfort, build quality, and versatile performance.\n\n` +
        `Shall we proceed with this pair? Just say **"Buy the ${topChoice.name}"** or **"Okay, buy them"** to confirm!`;

      return {
        message: responseText,
        activityLogs,
        rawProtocolRequest: null,
        gatewayResponse: null,
        protocolMetadata: null
      };
    }

    // --- CASE D: Informational / Price Quote Only ---
    // Strict Rule: DO NOT call checkout(), DO NOT create protocol request, DO NOT create Razorpay order!
    if (isPriceOnlyIntent) {
      const targetProduct = stateProduct || allProducts[0];
      const totalPrice = targetProduct.price * stateQuantity;

      activityLogs.push({
        tool: 'search_products',
        status: 'success',
        result: { product: targetProduct.name, price: targetProduct.price, quantity: stateQuantity }
      });

      responseText = `The **${targetProduct.name}** is priced at **₹${targetProduct.price.toLocaleString('en-IN')}** per pair.\n\n` +
        `For **${stateQuantity} pair${stateQuantity > 1 ? 's' : ''}**, the total is **₹${totalPrice.toLocaleString('en-IN')}**.\n\n` +
        `**Product Details:**\n` +
        `* **Category:** ${targetProduct.category}\n` +
        `* **Stock Status:** ${targetProduct.stock > 0 ? `${targetProduct.stock} pairs available` : 'Out of stock'}\n` +
        `* **Sizes Available:** ${targetProduct.sizes.join(', ')}\n\n` +
        `Would you like me to proceed with the purchase? Just say **"Okay, buy them"**!`;

      return {
        message: responseText,
        activityLogs,
        rawProtocolRequest: null,
        gatewayResponse: null,
        protocolMetadata: null
      };
    }

    // --- CASE E: Cart Operation Only ---
    if (isCartOnlyIntent) {
      const targetProduct = stateProduct || allProducts[0];
      activityLogs.push({
        tool: 'add_to_cart',
        status: 'running',
        args: { id: targetProduct.id, quantity: stateQuantity, size: stateSize }
      });

      const cartRes = await toolHandlers.addToCart({
        id: targetProduct.id,
        quantity: stateQuantity,
        size: stateSize
      });

      activityLogs.push({
        tool: 'add_to_cart',
        status: cartRes.success ? 'success' : 'failed',
        result: cartRes
      });

      responseText = `Added **${stateQuantity}x ${targetProduct.name}** (Size ${stateSize}, ₹${(targetProduct.price * stateQuantity).toLocaleString('en-IN')}) to your cart.\n\nSay **"Checkout"** or **"Buy with ${protocolId.toUpperCase()}"** whenever you are ready!`;

      return {
        message: responseText,
        activityLogs,
        rawProtocolRequest: null,
        gatewayResponse: null,
        protocolMetadata: null
      };
    }

    // --- CASE F: Transaction / Purchase Intent ONLY ---
    // Strict Rule: Protocol request is generated ONLY when purchase intent is active!
    if (isTransactionIntent) {
      // Check if budget is specified with buy command
      const budgetMatch = text.match(/(?:under|below|less than|<)\s*(?:₹|rs\.?|inr)?\s*([\d,]+)/i);
      if (budgetMatch && !currentProductMatch) {
        const budget = parseInt(budgetMatch[1].replace(/,/g, ''), 10);
        const withinBudget = allProducts.filter(p => p.price <= budget).sort((a, b) => b.price - a.price);
        if (withinBudget.length > 0) {
          stateProduct = withinBudget[0];
        }
      }

      const targetProduct = stateProduct || allProducts[0];

      // 1. Add to cart with exact state
      activityLogs.push({
        tool: 'add_to_cart',
        status: 'running',
        args: { id: targetProduct.id, quantity: stateQuantity, size: stateSize }
      });

      await toolHandlers.addToCart({
        id: targetProduct.id,
        quantity: stateQuantity,
        size: stateSize
      });

      activityLogs.push({
        tool: 'add_to_cart',
        status: 'success',
        result: { product: targetProduct.name, quantity: stateQuantity, size: stateSize }
      });

      // 2. Generate protocol request and execute Gateway checkout
      activityLogs.push({
        tool: 'checkout',
        status: 'running',
        args: { protocol: protocolId },
        message: `Generating genuine ${protocolId.toUpperCase()} request and executing Gateway checkout...`
      });

      const checkoutRes = await toolHandlers.executeCheckout(protocolId);
      activityLogs.push({
        tool: 'checkout',
        status: checkoutRes.success ? 'success' : 'failed',
        result: checkoutRes
      });

      if (checkoutRes.raw_protocol_payload) {
        rawProtocolRequest = checkoutRes.raw_protocol_payload;
        protocolMetadata = {
          id: checkoutRes.protocol,
          name: checkoutRes.protocol_name,
          version: checkoutRes.protocol_version,
          status_notice: checkoutRes.status_notice
        };
      }
      if (checkoutRes.gateway_data) {
        gatewayResponse = checkoutRes.gateway_data;
      }

      const totalFormatted = (targetProduct.price * stateQuantity).toLocaleString('en-IN');

      if (checkoutRes.success && checkoutRes.protocol === 'x402') {
        const orderId = checkoutRes.gateway_data?.razorpay_order_id || 'order_created';
        const p = checkoutRes.raw_protocol_payload;
        responseText = `### 🎯 x402 v2 Order Authorized & Prepared!\n\n` +
          `I have selected **${stateQuantity}x ${targetProduct.name}** (Size ${stateSize}, ₹${totalFormatted}) and normalized the genuine **x402 v2 PaymentPayload** through the **Agent Commerce Gateway**.\n\n` +
          `**Protocol & Gateway Summary**:\n` +
          `- **Protocol**: \`x402 v2 (HTTP 402 Pay-per-Request)\`\n` +
          `- **Network & Scheme**: \`${p?.accepted?.network || 'eip155:84532'}\` (Base Sepolia) | \`${p?.accepted?.scheme || 'exact'}\`\n` +
          `- **Gateway Decision**: 🛡️ \`ALLOW\`\n` +
          `- **Order ID**: \`${orderId}\`\n\n` +
          `The x402 v2 payment payload was normalized into the canonical model, verified against replay and merchant policy, and is now ready for payment handoff!`;
      } else if (checkoutRes.status_notice === 'PROTOCOL_EXPERIMENTAL' || checkoutRes.status_notice === 'PROTOCOL_UNSUPPORTED') {
        responseText = `### 📋 ${checkoutRes.protocol_name} Notice\n\n` +
          `> ⚠️ **Notice**: **${checkoutRes.protocol_name}** is currently unsupported.\n\n` +
          `Please use **ACP (RFC 2026-01-16)** or **x402 v2** for supported protocol interactions.`;
      } else if (checkoutRes.success) {
        const orderId = checkoutRes.gateway_data?.razorpay_order_id || 'order_created';
        responseText = `### 🎯 Order Authorized & Prepared!\n\n` +
          `I have selected **${stateQuantity}x ${targetProduct.name}** (Size ${stateSize}, ₹${totalFormatted}) as requested.\n\n` +
          `**Protocol Action Summary**:\n` +
          `- **Selected Protocol**: \`${protocolId.toUpperCase()} (2026)\`\n` +
          `- **Gateway Decision**: 🛡️ \`ALLOW\`\n` +
          `- **Order ID**: \`${orderId}\`\n\n` +
          `The genuine protocol request was verified and normalized by the **Agent Commerce Gateway**. The transaction is now ready for payment handoff!`;
      } else {
        responseText = `### ⚠️ Transaction Blocked by Gateway Policy\n\n` +
          `I selected **${stateQuantity}x ${targetProduct.name}** (Size ${stateSize}) and generated the **${protocolId.toUpperCase()}** request, but the **Agent Commerce Gateway** returned a security restriction:\n\n` +
          `> **Reason**: ${checkoutRes.error || 'Policy threshold exceeded'}\n\n` +
          `Review the live security pipeline in the sidebar for details.`;
      }

      return {
        message: responseText,
        activityLogs,
        rawProtocolRequest,
        gatewayResponse,
        protocolMetadata
      };
    }

    // ── CASE G: PRODUCT SEARCH & BROWSING ONLY (INFORMATIONAL) ───────────────
    // User wants to discover or search specific categories or budgets.
    let matchedCategory = null;
    if (text.includes('running')) matchedCategory = 'Running';
    else if (text.includes('casual') || text.includes('sneaker')) matchedCategory = 'Casual';
    else if (text.includes('formal') || text.includes('oxford')) matchedCategory = 'Formal';
    else if (text.includes('outdoor') || text.includes('water')) matchedCategory = 'Outdoor';

    let budgetLimit = null;
    const bMatch = text.match(/(?:under|below|less than|<)\s*(?:₹|rs\.?|inr)?\s*([\d,]+)/i);
    if (bMatch) {
      budgetLimit = parseInt(bMatch[1].replace(/,/g, ''), 10);
    }

    let matching = allProducts;
    if (matchedCategory) {
      matching = matching.filter(p => p.category.toLowerCase() === matchedCategory.toLowerCase());
    }
    if (budgetLimit !== null) {
      matching = matching.filter(p => p.price <= budgetLimit);
    }
    if (matching.length === 0 && budgetLimit !== null) {
      matching = allProducts.filter(p => p.price <= budgetLimit);
    }
    if (matching.length === 0) {
      matching = allProducts.slice(0, 3);
    } else {
      matching = matching.slice(0, 3); // Keep it concise and curated
    }

    activityLogs.push({
      tool: 'search_products',
      status: 'success',
      args: { query: userMessage, category: matchedCategory || '', max_price: budgetLimit || '' },
      result: { count: matching.length, products: matching.map(p => p.name) }
    });

    const headerTitle = budgetLimit 
      ? `### 🔍 ${matchedCategory ? matchedCategory + ' Shoes' : 'Shoes'} under ₹${budgetLimit.toLocaleString('en-IN')}`
      : `### 👟 ${matchedCategory ? matchedCategory + ' Shoes' : 'Recommended Shoes'}`;

    const prodsList = matching.map(p => 
      `- **${p.name}** (${p.category}) — **₹${p.price.toLocaleString('en-IN')}** | Stock: ${p.stock} | Sizes: ${p.sizes.join(', ')}`
    ).join('\n');

    return {
      message: `${headerTitle}\n\nHere are the top options from our catalog:\n${prodsList}\n\nWhich pair would you like? (Say **"Buy the ${matching[0].name}"** to purchase or **"Choose 1 for me"**)!`,
      activityLogs,
      rawProtocolRequest: null,
      gatewayResponse: null,
      protocolMetadata: null
    };
  }
}

module.exports = new AIAgentEngine();
