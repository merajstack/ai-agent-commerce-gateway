const config = require('./config');
const toolDefinitions = require('./tools/definitions');
const toolHandlers = require('./tools/handlers');
const { generateProtocolPayload } = require('./protocols');

/**
 * AI Shopping Agent Engine — Apex Footwear
 * 
 * Features:
 * - Conversational, witty, intelligent shopping companion & general talk assistant
 * - Full knowledge base of Apex Footwear (10 shoe products, inventory, prices, specs, policies)
 * - Open-domain conversational ability (jokes, general talk, fashion advice, sizing, running tips, tech)
 * - Gemini Flash models with Function Calling + Multi-Turn Context Tracking
 * - Deterministic fallback planner with complete store dataset & smart conversational Q&A
 */
class AIAgentEngine {
  resetState() {
    this.state = {
      selected_product: null,
      selected_quantity: 1,
      selected_size: null,
      cart_confirmed: false,
      pending_checkout: false
    };
  }

  constructor() {
    this.resetState();
    this.models = [
      config.GEMINI_MODEL || 'gemini-3.5-flash',
      'gemini-3.5-flash',
      'gemini-3.5-flash-lite',
      'gemini-3.6-flash'
    ];

    this.systemInstruction = `You are an intelligent, friendly, and conversational AI Shopping Assistant for Apex Footwear (https://apexfootwear.com), powered by the Agent Commerce Gateway.
You speak like a knowledgeable footwear stylist, athlete advisor, and helpful everyday AI assistant.

=== 1. NATURAL CONVERSATION & GENERAL TALK ===
- You can chat casually, answer general knowledge questions, tell lighthearted jokes, give styling & shoe care advice, explain athletic training/running tips, and explain commerce technologies (like AI, agentic commerce, ACP, x402).
- When the user asks general questions or initiates casual talk (e.g. "hey", "how are you?", "tell me a joke", "what is machine learning?", "who are you?", "what is ACP?"), answer warmly, helpfully, and naturally in conversational prose.
- Do NOT force tool calls or dump catalog listings for simple general conversation, jokes, or greetings.

=== 2. COMPLETE STORE & CATALOG DATA (APEX FOOTWEAR) ===
- Brand: Apex Footwear — Ultra-Premium Modern Footwear Storefront.
- Policies:
  • Free express delivery across India (2–4 business days delivery).
  • 30-day hassle-free returns and instant size exchanges on unworn pairs.
  • 100% genuine handcrafted & certified authentic footwear.
  • Sizing: Standard UK / India shoe sizes (Size 5 to Size 12).
- Technology & Payment Gateway:
  • Integrated with Agent Commerce Gateway for cryptographic protocol-agnostic settlement.
  • Supported Protocols: ACP (Agentic Commerce Protocol, RFC 2026-01-16) and x402 v2 (HTTP 402 Pay-per-Request).
  • Payment Settlement: Razorpay Test Mode Sandbox.

- COMPLETE PRODUCT CATALOG (10 Products):
  1. [prod_shoe_001] "AeroGlide Runner" — ₹8,500 | Category: Running | Stock: 45 | Sizes: 7, 8, 9, 10, 11
     • Specs: Ultra-lightweight engineered mesh, responsive high-rebound midsole, maximum energy return. Best for daily road runs, marathon training, and high-tempo fitness.
  2. [prod_shoe_002] "Urban Kicks Classic" — ₹5,200 | Category: Casual | Stock: 120 | Sizes: 6, 7, 8, 9, 10
     • Specs: Heavy-duty canvas upper, vintage skate silhouette, padded collar, vulcanized non-slip rubber outsole. Best for everyday street style and all-day comfort.
  3. [prod_shoe_003] "Trail Blazer GTX" — ₹11,000 | Category: Outdoor / Hiking | Stock: 12 | Sizes: 8, 9, 10, 11, 12
     • Specs: 100% waterproof Gore-Tex (GTX) membrane, reinforced rock-plate toe cap, high-traction Vibram multi-terrain lug outsole. Best for mountain hiking and wet muddy trails.
  4. [prod_shoe_004] "Court Master Pro" — ₹7,800 | Category: Basketball | Stock: 30 | Sizes: 9, 10, 11, 12
     • Specs: High-top dynamic ankle collar with lockdown strap, herringbone court traction pattern, responsive forefoot air cushion. Best for basketball and indoor court agility.
  5. [prod_shoe_005] "Slip-On Comfort" — ₹3,500 | Category: Casual | Stock: 200 | Sizes: 5, 6, 7, 8, 9, 10
     • Specs: Breathable knit textile, dual-density memory foam insole, elastic dual-side stretch gussets. Best budget-friendly everyday walking shoe.
  6. [prod_shoe_006] "Sprint Spike 300" — ₹9,500 | Category: Running / Track | Stock: 0 (OUT OF STOCK) | Sizes: 7, 8, 9
     • Specs: Elite 6-spike Pebax sprint plate, ultra-thin aerodynamic synthetic upper. Best for competitive 100m–400m track sprints.
  7. [prod_shoe_007] "Leather Oxford Elite" — ₹14,500 | Category: Formal | Stock: 25 | Sizes: 8, 9, 10, 11
     • Specs: Handcrafted full-grain Italian calfskin leather, Goodyear welted construction, hand-burnished finish. Best for formal black-tie events, weddings, and executive boardrooms.
  8. [prod_shoe_008] "Aqua Walker" — ₹4,200 | Category: Outdoor / Water | Stock: 85 | Sizes: 6, 7, 8, 9, 10, 11
     • Specs: Hydrophobic quick-dry mesh with drainage ports, razor-siped anti-slip rubber outsole, protective rubber bumper. Best for water sports, beach, kayaking, and monsoon.
  9. [prod_shoe_009] "Velocity Nitro" — ₹10,200 | Category: Running / Racing | Stock: 50 | Sizes: 7, 8, 9, 10, 11
     • Specs: Full-length curved carbon-composite propulsion plate, supercritical nitrogen-infused Nitro foam for explosive forward thrust. Best for racing and marathon PBs.
  10. [prod_shoe_010] "Suede Loafer" — ₹6,800 | Category: Casual / Formal | Stock: 40 | Sizes: 7, 8, 9, 10
      • Specs: Supple Italian calf suede, leather lining, hand-stitched apron, cushioned footbed. Best for smart-casual evenings, dinners, and driving.

=== 3. CONVERSATIONAL COMMERCE GUIDELINES ===
- GREETINGS & CASUAL TALK: If the user says "hey", "hello", "hi", "how are you":
  → Answer naturally: "I’m doing well! What would you like to shop for?" (or friendly greeting).
- RECOMMENDATIONS: When asked for advice, recommend 1 or 2 top specific items with concise reasons.
- PRICE INQUIRIES: Accurately calculate unit price × quantity (e.g. 3 AeroGlide = 3 × ₹8,500 = ₹25,500) and invite them to purchase.
- PURCHASE INTENT ("buy AeroGlide size 9", "checkout with ACP", "okay buy them"):
  → Call add_to_cart and checkout tools to generate genuine protocol payloads and execute Gateway authorization.
`;
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
          temperature: 0.3,
          maxOutputTokens: 1024
        }
      };

      const response = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(requestBody),
        signal: AbortSignal.timeout(6000)
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
      const textParts = parts.filter(p => p.text && !p.thought).map(p => p.text);

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
   * Deterministic Autonomous Planner & Knowledge Engine
   * Equipped with the entire shoe store dataset and conversational intelligence:
   * 1. Greetings & general casual talk (jokes, general queries, sizing, shoe care, technology, open talk)
   * 2. Store FAQs & technical shoe specs (Gore-Tex, carbon plate, materials, warranty, returns)
   * 3. Curated recommendations with expert reasoning
   * 4. Decision delegation ("choose 1", "pick for me")
   * 5. Price quotes with accurate multi-pair calculations
   * 6. Direct purchase & protocol checkout execution (ACP / x402)
   */
  async _runDeterministicPlanner(userMessage, history = [], protocolId = 'acp', activityLogs = []) {
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

    // Full 10-product Catalog Dataset
    const catalogRes = await toolHandlers.searchProducts();
    let allProducts = catalogRes.products || [];
    if (!allProducts || allProducts.length === 0) {
      allProducts = [
        { id: 'prod_shoe_001', name: 'AeroGlide Runner', price: 8500, category: 'Running', stock: 45, sizes: ['7', '8', '9', '10', '11'], description: 'Ultra-lightweight road running shoe with maximum energy return foam.' },
        { id: 'prod_shoe_002', name: 'Urban Kicks Classic', price: 5200, category: 'Casual', stock: 120, sizes: ['6', '7', '8', '9', '10'], description: 'Timeless canvas street sneaker with vulcanized rubber sole.' },
        { id: 'prod_shoe_003', name: 'Trail Blazer GTX', price: 11000, category: 'Outdoor', stock: 12, sizes: ['8', '9', '10', '11', '12'], description: '100% waterproof trail shoe with Gore-Tex (GTX) membrane and Vibram lugs.' },
        { id: 'prod_shoe_004', name: 'Court Master Pro', price: 7800, category: 'Basketball', stock: 30, sizes: ['9', '10', '11', '12'], description: 'High-performance basketball shoe with dynamic ankle support and air cushioning.' },
        { id: 'prod_shoe_005', name: 'Slip-On Comfort', price: 3500, category: 'Casual', stock: 200, sizes: ['5', '6', '7', '8', '9', '10'], description: 'Ultra-flexible everyday slip-on with dual-density memory foam.' },
        { id: 'prod_shoe_006', name: 'Sprint Spike 300', price: 9500, category: 'Running', stock: 0, sizes: ['7', '8', '9'], description: 'Elite 6-spike track competition shoe with Pebax propulsion plate. Out of stock.' },
        { id: 'prod_shoe_007', name: 'Leather Oxford Elite', price: 14500, category: 'Formal', stock: 25, sizes: ['8', '9', '10', '11'], description: 'Handcrafted luxury formal oxford in full-grain Italian calfskin.' },
        { id: 'prod_shoe_008', name: 'Aqua Walker', price: 4200, category: 'Outdoor', stock: 85, sizes: ['6', '7', '8', '9', '10', '11'], description: 'Hydrophobic quick-dry water shoe with razor-siped non-slip grip.' },
        { id: 'prod_shoe_009', name: 'Velocity Nitro', price: 10200, category: 'Running', stock: 50, sizes: ['7', '8', '9', '10', '11'], description: 'Marathon racing shoe with carbon-composite plate and nitrogen foam.' },
        { id: 'prod_shoe_010', name: 'Suede Loafer', price: 6800, category: 'Casual', stock: 40, sizes: ['7', '8', '9', '10'], description: 'Sophisticated penny loafer in supple Italian calf suede.' }
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

    // Helper: Extract quantity
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
      if (!str) return null;
      const match = str.match(/size\s*[:\s]?\s*(\d+)/i);
      return match ? match[1] : null;
    };

    // ── 0. GREETINGS & SIMPLE TALK ──────────────────────────────────────────
    const isPureGreeting = /^(hi|hello|hey|howdy|greetings|good\s+(?:morning|afternoon|evening)|yo|hey\s+there)\b/i.test(text) ||
                           /\b(how\s+are\s+you|how's\s+it\s+going|how\s+do\s+you\s+do|what'?s\s+up)\b/i.test(text);
    const hasShoppingKeywords = /\b(shoe|shoes|sneaker|sneakers|runner|kicks|loafer|boots|buy|purchase|order|cart|checkout|price|cost|total|find|search|catalog|recommend|stock|size|under|below|₹|\$|choose|pick|select|oxford|suede|gtx|waterproof|trail|marathon|running|casual|formal|outdoor)\b/i.test(text);

    if (isPureGreeting && !hasShoppingKeywords) {
      return {
        message: "I’m doing well! What would you like to shop for?",
        activityLogs: [],
        rawProtocolRequest: null,
        gatewayResponse: null,
        protocolMetadata: null
      };
    }

    // Jokes & Humor
    if (/\b(tell\s+me\s+a\s+joke|make\s+me\s+laugh|say\s+something\s+funny|joke|pun)\b/i.test(text) && !hasShoppingKeywords) {
      const jokes = [
        "Why did the shoe go to school? Because it wanted to be a little sneaker! 😄👟 What can I help you find today at Apex Footwear?",
        "Why are shoes always so calm and relaxed? Because they have good soles! 😄 How can I help you today?",
        "What do runners eat before a big race? Nothing, they fast! 🏃💨 Looking for a fresh pair of running shoes?"
      ];
      return {
        message: jokes[Math.floor(Math.random() * jokes.length)],
        activityLogs: [],
        rawProtocolRequest: null,
        gatewayResponse: null,
        protocolMetadata: null
      };
    }

    // Bot Identity & General Questions
    if (/\b(who\s+(?:are|made|created)\s+you|what\s+is\s+your\s+name|what\s+are\s+you|what\s+can\s+you\s+do|introduce\s+yourself|tell\s+me\s+about\s+yourself)\b/i.test(text) && !text.includes('buy')) {
      return {
        message: `I am the autonomous AI Shopping Agent for **Apex Footwear**, powered by the **Agent Commerce Gateway**! 🤖👟\n\n` +
                 `Here is what I can do for you:\n` +
                 `• **Explore & Recommend**: Help you find running, casual, outdoor, formal, or basketball shoes.\n` +
                 `• **Check Real-Time Data**: Instant specs, pricing in INR, sizes, and stock availability.\n` +
                 `• **Shoe Sizing & Care Advice**: Advice on sizing, Gore-Tex care, suede cleaning, and running ergonomics.\n` +
                 `• **Autonomous Execution**: Safely generate protocol-compliant **ACP** or **x402 v2** requests and execute checkout!\n\n` +
                 `Feel free to ask me anything or say **"Show all shoes"**!`,
        activityLogs: [],
        rawProtocolRequest: null,
        gatewayResponse: null,
        protocolMetadata: null
      };
    }

    // Protocol Information (ACP / x402 / Gateway)
    if (/\b(what\s+is\s+acp|what\s+is\s+x402|how\s+does\s+the\s+gateway\s+work|what\s+is\s+agentic\s+commerce)\b/i.test(text)) {
      return {
        message: `### 🌐 Protocol-Agnostic Commerce Infrastructure\n\n` +
                 `• **ACP (Agentic Commerce Protocol, RFC 2026-01-16)**: An open standard for autonomous agent purchase orders with opaque bearer authorization and idempotency headers.\n` +
                 `• **x402 v2 (HTTP 402 / Base Sepolia)**: Standardized pay-per-request protocol using cryptographic EIP-712/EIP-3009 signatures on Base Sepolia testnet (\`eip155:84532\`).\n` +
                 `• **Agent Commerce Gateway**: Sits between AI Buyers and merchants, normalizes diverse protocol formats into one canonical \`CommerceRequest\`, verifies replay protection & merchant policies, and executes approved orders via **Razorpay Test Mode**!`,
        activityLogs: [],
        rawProtocolRequest: null,
        gatewayResponse: null,
        protocolMetadata: null
      };
    }

    // Weather & Casual Chat
    if (/\b(weather|temperature|forecast|rainy|sunny|cold|hot)\b/i.test(text) && !hasShoppingKeywords) {
      return {
        message: `I don't have real-time live weather feeds, but whatever the weather is outside, we've got you covered! ☀️🌧️\n\n` +
                 `• **Rain & Mud**: Check out our waterproof **Trail Blazer GTX** (₹11,000) with Gore-Tex.\n` +
                 `• **Sunny Runs**: The breathable **AeroGlide Runner** (₹8,500) keeps feet cool.\n` +
                 `• **Monsoon & Beach**: The quick-drain **Aqua Walker** (₹4,200) is built for water activities.\n\n` +
                 `What kind of weather are you stepping out into?`,
        activityLogs: [],
        rawProtocolRequest: null,
        gatewayResponse: null,
        protocolMetadata: null
      };
    }

    // Technology / AI / Science Chat
    if (/\b(what\s+is\s+(?:ai|artificial\s+intelligence|machine\s+learning|ml|llm)|tell\s+me\s+about\s+ai)\b/i.test(text)) {
      return {
        message: `Artificial Intelligence (AI) and Machine Learning enable computers to understand natural language, reason over complex tasks, and act autonomously. 🤖\n\n` +
                 `Here at **Apex Footwear**, I use conversational AI integrated with the **Agent Commerce Gateway** to negotiate, verify, and execute purchases across open agentic protocols like **ACP** and **x402 v2**!`,
        activityLogs: [],
        rawProtocolRequest: null,
        gatewayResponse: null,
        protocolMetadata: null
      };
    }

    // Store Policies: Shipping, Returns, Exchanges
    if (/\b(return\s+policy|exchange\s+policy|shipping\s+time|delivery\s+time|how\s+long\s+does\s+shipping\s+take|warranty)\b/i.test(text)) {
      return {
        message: `### 📦 Apex Footwear Store Policies\n\n` +
                 `• 🚚 **Express Shipping**: Free express delivery across India in **2–4 business days** on all orders.\n` +
                 `• 🔄 **30-Day Easy Returns**: 30-day hassle-free return and size exchange policy for all unworn pairs with original tags.\n` +
                 `• 🛡️ **100% Genuine Guarantee**: All footwear is handcrafted and certified authentic with manufacturer warranty.\n` +
                 `• 💳 **Secure Payment**: Integrated with **Agent Commerce Gateway** and **Razorpay Sandbox**!`,
        activityLogs: [],
        rawProtocolRequest: null,
        gatewayResponse: null,
        protocolMetadata: null
      };
    }

    // Shoe Care & Maintenance (Suede, Leather, Cleaning)
    if (/\b(how\s+to\s+clean|care\s+for|clean\s+suede|clean\s+leather|maintenance)\b/i.test(text)) {
      if (text.includes('suede')) {
        return {
          message: `### 👞 Suede Shoe Care Guide (e.g. Suede Loafer)\n\n` +
                   `1. **Dry Brushing**: Use a soft-bristle suede brush in one direction to lift dirt and restore nap.\n` +
                   `2. **Spot Cleaning**: Use a dedicated suede eraser for dry scuffs. Avoid liquid water on suede!\n` +
                   `3. **Weatherproofing**: Spray with a silicone-free water-repellent suede protector before damp weather.\n` +
                   `4. **Storage**: Store with cedar shoe trees in breathable dust bags.`,
          activityLogs: [],
          rawProtocolRequest: null,
          gatewayResponse: null,
          protocolMetadata: null
        };
      }
      return {
        message: `### 👟 General Footwear Care Tips\n\n` +
                 `• **Running & Mesh (AeroGlide / Velocity Nitro)**: Wipe with a damp cloth and mild soap. Always air-dry away from direct heat.\n` +
                 `• **Gore-Tex / Trail (Trail Blazer GTX)**: Rinse off dried mud under lukewarm tap water. Reapply DWR water-repellent spray seasonally.\n` +
                 `• **Full-Grain Leather (Oxford Elite)**: Apply leather cream polish every 3–4 weeks and buff with a horsehair brush.\n` +
                 `• **Suede (Suede Loafer)**: Use a dry suede brush and avoid water soaking.`,
          activityLogs: [],
          rawProtocolRequest: null,
          gatewayResponse: null,
          protocolMetadata: null
        };
    }

    // Sizing & Fit Advice
    if (/\b(sizing|measure|fit\s+guide|what\s+size|which\s+size|wide\s+feet|shoe\s+sizes?)\b/i.test(text) && !text.match(/size\s*[:\s]?\s*\d+/i)) {
      return {
        message: `### 📏 Apex Footwear Sizing & Fit Guide\n\n` +
                 `We use **standard UK / India sizing** (Sizes 5 to 12):\n` +
                 `- **Size 6**: ~24.5 cm foot length\n` +
                 `- **Size 7**: ~25.5 cm foot length\n` +
                 `- **Size 8**: ~26.5 cm foot length\n` +
                 `- **Size 9**: ~27.5 cm foot length (Most Popular)\n` +
                 `- **Size 10**: ~28.5 cm foot length\n` +
                 `- **Size 11**: ~29.5 cm foot length\n\n` +
                 `💡 **Pro Tip**: If you have wider feet or are purchasing distance runners (**AeroGlide Runner** / **Velocity Nitro**), we recommend choosing **half a size up** for toe-box comfort!`,
        activityLogs: [],
        rawProtocolRequest: null,
        gatewayResponse: null,
        protocolMetadata: null
      };
    }

    // Specific Product Feature Searches (Waterproof, Carbon plate, Out of Stock, Luxury, Budget)
    if (/\b(waterproof|gore\s*tex|gtx|water\s+shoe|rain)\b/i.test(text) && !text.startsWith('buy')) {
      return {
        message: `### 🌧️ Waterproof & Water-Ready Shoes\n\n` +
                 `1. 🏔️ **Trail Blazer GTX** — **₹11,000** (Size 8–12 | Stock: 12)\n` +
                 `   • Features a 100% waterproof **Gore-Tex (GTX)** membrane and rugged multi-terrain Vibram lug sole for mountain hiking.\n\n` +
                 `2. 🌊 **Aqua Walker** — **₹4,200** (Size 6–11 | Stock: 85)\n` +
                 `   • Hydrophobic quick-dry water shoe with drainage ports and siped anti-slip rubber outsole for water sports & monsoon.\n\n` +
                 `Would you like to buy either pair? Say **"Buy Trail Blazer GTX"** or **"Buy Aqua Walker"**!`,
        activityLogs: [],
        rawProtocolRequest: null,
        gatewayResponse: null,
        protocolMetadata: null
      };
    }

    if (/\b(carbon\s+plate|marathon|fastest|super\s+shoe|nitro)\b/i.test(text) && !text.startsWith('buy')) {
      return {
        message: `### ⚡ Elite Racing & Marathon Footwear\n\n` +
                 `Our flagship race-day shoe is the **Velocity Nitro** (₹10,200) 🏆\n` +
                 `• **Full-Length Carbon-Composite Plate**: Delivers explosive propulsion with every stride.\n` +
                 `• **Nitrogen-Infused Nitro Foam**: Supercritical foam offering maximum energy return at minimal weight.\n` +
                 `• **Sizes**: 7, 8, 9, 10, 11 | **Stock**: 50 pairs available.\n\n` +
                 `For daily high-tempo training, our **AeroGlide Runner** (₹8,500) is also a fantastic companion! Ready to order? Say **"Buy Velocity Nitro in size 9"**!`,
        activityLogs: [],
        rawProtocolRequest: null,
        gatewayResponse: null,
        protocolMetadata: null
      };
    }

    // ── 1. MANAGE STATE ──────────────────────────────────────────────────────
    const currentCart = await toolHandlers.getCart();

    const currentProductMatch = findProductInText(text);
    if (currentProductMatch) {
      if (!this.state.selected_product || this.state.selected_product.id !== currentProductMatch.id) {
        this.state.selected_size = null; // Reset size for new product
      }
      this.state.selected_product = currentProductMatch;
    }
    
    const hasExplicitQuantityInCurrentMsg = /(?:\b\d+\s*(?:pairs?|items?|shoes?|x\b|pcs?)|(?:get|need|want|buy|order|for|take)\s+\d+\b)/i.test(text);
    if (hasExplicitQuantityInCurrentMsg) {
      this.state.selected_quantity = extractQuantity(text);
    }
    
    if (text.includes('size')) {
      const s = extractSize(text);
      if (s) {
        this.state.selected_size = s;
      }
    }

    let stateProduct = this.state.selected_product;
    let stateQuantity = this.state.selected_quantity;
    let stateSize = this.state.selected_size;

    // ── 2. CLASSIFY SHOPPING INTENT ──────────────────────────────────────────
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
    let isTransactionIntent = !isPriceOnlyIntent && (isConfirmPhrase || isDirectBuy);
    
    if (this.state.pending_checkout && text.includes('size')) {
      isTransactionIntent = true;
      this.state.pending_checkout = false;
    }

    const isCartViewIntent = text.includes('cart') && (text.includes('show') || text.includes('view') || text.includes('what'));
    const isChooseForMeIntent = /\b(choose\s*(?:one|1)?|pick\s*(?:one|1)?|select\s*(?:one|1)?|you\s+choose|you\s+pick|which\s+one\s+should\s+i|your\s+choice|decide\s+for\s+me)\b/i.test(text) && !isTransactionIntent;
    const isRecommendationIntent = /\b(recommend|recommendation|suggest|suggestion|best\s+shoe|best\s+one|top\s+pick|favorite|favourite|what\s+do\s+you\s+suggest|what\s+should\s+i\s+buy)\b/i.test(text) && !isTransactionIntent && !isChooseForMeIntent;

    // ── 3. EXECUTE SHOPPING INTENT ───────────────────────────────────────────

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

      responseText = `Here are my **top two picks** depending on your lifestyle:\n\n` +
        `1. 🏃 **${topRunning.name}** (₹${topRunning.price.toLocaleString('en-IN')}) — Our **#1 running shoe**. Ultra-lightweight with high-rebound cushioning for road running and training.\n` +
        `2. 👟 **${topCasual.name}** (₹${topCasual.price.toLocaleString('en-IN')}) — Perfect for **everyday casual wear**. Vintage street aesthetic with durable vulcanized comfort.\n\n` +
        `💡 *Looking for great value under ₹5,000?* The **${topOutdoor.name}** (₹${topOutdoor.price.toLocaleString('en-IN')}) is also fantastic for outdoor versatility.\n\n` +
        `Which one catches your eye? Say **"Choose 1 for me"**, **"Tell me the price for 2"**, or **"Buy the ${topRunning.name}"**!`;

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
      this.state.selected_product = topChoice;
      stateProduct = topChoice;

      activityLogs.push({
        tool: 'get_product',
        status: 'success',
        args: { id: topChoice.id },
        result: { selected: topChoice.name, price: topChoice.price }
      });

      responseText = `If I had to pick just one, I definitely recommend the **${topChoice.name}** (₹${topChoice.price.toLocaleString('en-IN')}${stateSize ? `, Size ${stateSize}` : ''})! 🏆\n\n` +
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
    if (isPriceOnlyIntent) {
      const targetProduct = stateProduct;
      if (!targetProduct) {
        return {
          message: "Could you clarify which product's price you'd like to check?",
          activityLogs,
          rawProtocolRequest: null,
          gatewayResponse: null,
          protocolMetadata: null
        };
      }
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
      const targetProduct = stateProduct;
      if (!targetProduct) {
        return {
          message: "Which product would you like to add to your cart?",
          activityLogs,
          rawProtocolRequest: null,
          gatewayResponse: null,
          protocolMetadata: null
        };
      }
      if (!stateSize) {
        return {
          message: `What size would you like for the **${targetProduct.name}**?`,
          activityLogs,
          rawProtocolRequest: null,
          gatewayResponse: null,
          protocolMetadata: null
        };
      }
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
    if (isTransactionIntent) {
      const budgetMatch = text.match(/(?:under|below|less than|<)\s*(?:₹|rs\.?|inr)?\s*([\d,]+)/i);
      if (budgetMatch && !currentProductMatch) {
        const budget = parseInt(budgetMatch[1].replace(/,/g, ''), 10);
        const withinBudget = allProducts.filter(p => p.price <= budget).sort((a, b) => b.price - a.price);
        if (withinBudget.length > 0) {
          stateProduct = withinBudget[0];
          this.state.selected_product = stateProduct;
        }
      }

      const targetProduct = stateProduct;

      if (!targetProduct) {
        return {
          message: "Which product would you like to buy?",
          activityLogs,
          rawProtocolRequest: null,
          gatewayResponse: null,
          protocolMetadata: null
        };
      }

      if (!stateSize) {
        this.state.pending_checkout = true;
        return {
          message: `What size would you like for the **${targetProduct.name}**?`,
          activityLogs,
          rawProtocolRequest: null,
          gatewayResponse: null,
          protocolMetadata: null
        };
      }

      // 1. Add to cart
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

    // ── CASE G: CATALOG SEARCH & PRODUCT BROWSING ────────────────────────────
    let matchedCategory = null;
    if (text.includes('running')) matchedCategory = 'Running';
    else if (text.includes('casual') || text.includes('sneaker')) matchedCategory = 'Casual';
    else if (text.includes('formal') || text.includes('oxford')) matchedCategory = 'Formal';
    else if (text.includes('outdoor') || text.includes('water')) matchedCategory = 'Outdoor';
    else if (text.includes('basketball') || text.includes('court')) matchedCategory = 'Basketball';

    let budgetLimit = null;
    const bMatch = text.match(/(?:under|below|less than|<)\s*(?:₹|rs\.?|inr)?\s*([\d,]+)/i);
    if (bMatch) {
      budgetLimit = parseInt(bMatch[1].replace(/,/g, ''), 10);
    }

    const isSearchQuery = /\b(find|search|show|list|catalog|shoes?|sneakers?|browse|collection|options|all|under|below|less)\b/i.test(text) || matchedCategory || budgetLimit !== null;

    // Open-domain casual conversation fallback
    if (!isSearchQuery && !currentProductMatch) {
      return {
        message: `I'm happy to chat about footwear styles, running training, shoe care, or help you explore the **Apex Footwear** collection! 👟✨\n\n` +
                 `Feel free to ask me questions like:\n` +
                 `• *"Show me running shoes"* \n` +
                 `• *"Which shoes are waterproof?"*\n` +
                 `• *"How do I measure my shoe size?"*\n` +
                 `• *"Tell me a joke!"*`,
        activityLogs: [],
        rawProtocolRequest: null,
        gatewayResponse: null,
        protocolMetadata: null
      };
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
      matching = matching.slice(0, 3);
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
