'use client';

import React, { useEffect, useRef, useState } from 'react';

// Tipos do chat
export type Msg = { role: 'user' | 'assistant'; content: string };
type Citation = { uri: string; score?: number | null };

// Caminho da API (mantenha o rewrite no next.config.ts apontando para seu Flask /chat)
const API_PATH = '/api/chat';

// ---------- helpers ----------
type Source = { title: string; url?: string | null };
type TestCase = {
  id: string;
  title: string;
  type: 'functional' | 'negative' | 'nonfunctional';
  steps: string[];
  expected_result: string;
  tags: string[];
  traceability: string[];
};
type HybridJSON = {
  summary?: string;
  artifacts?: {
    test_cases?: TestCase[];
    acceptance_criteria?: string[];
    validation_checklist?: string[];
    risks?: string[];
    open_questions?: string[];
  };
  sources?: Source[];
};

// Renderiza o JSON híbrido em texto legível dentro da bolha
function renderHybridToText(j: HybridJSON): string {
  const parts: string[] = [];
  if (j.summary) {
    parts.push(`**Resumo**\n${j.summary}\n`);
  }
  const ac = j.artifacts?.acceptance_criteria ?? [];
  const tcs = j.artifacts?.test_cases ?? [];
  const chk = j.artifacts?.validation_checklist ?? [];
  const risks = j.artifacts?.risks ?? [];
  const oq = j.artifacts?.open_questions ?? [];

  if (tcs.length) {
    parts.push(`**Casos de Teste**`);
    tcs.forEach((tc) => {
      parts.push(
        `- **${tc.id} — ${tc.title}** (${tc.type})\n` +
          `  Passos:\n` +
          `  ${tc.steps.map((s, i) => `${i + 1}. ${s}`).join('\n  ')}\n` +
          `  Resultado Esperado: ${tc.expected_result}\n` +
          `  Tags: ${tc.tags?.join(', ') || '-'}\n` +
          `  Rastreabilidade: ${tc.traceability?.join(', ') || '-'}\n`
      );
    });
  }

  if (ac.length) {
    parts.push(`**Critérios de Aceitação**\n- ${ac.join('\n- ')}`);
  }
  if (chk.length) {
    parts.push(`**Checklist de Validação**\n- ${chk.join('\n- ')}`);
  }
  if (risks.length) {
    parts.push(`**Riscos**\n- ${risks.join('\n- ')}`);
  }
  if (oq.length) {
    parts.push(`**Perguntas em Aberto**\n- ${oq.join('\n- ')}`);
  }

  return parts.filter(Boolean).join('\n\n');
}

function extractCitations(j: HybridJSON): Citation[] {
  const src = j.sources ?? [];
  return src.map((s) => ({ uri: s.url || s.title || 'Fonte' }));
}

// Lê texto de erro do Response sem quebrar se não for body-text
async function safeReadText(res: Response) {
  try {
    return await res.text();
  } catch {
    return res.statusText || 'Erro HTTP';
  }
}

// ---------- componente ----------
export default function BedrockClaudeChat() {
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [messages, setMessages] = useState<Msg[]>([
    {
      role: 'assistant',
      content:
        'Olá! Envie sua pergunta. Vou usar a base (KB) e complementar criativamente quando necessário.',
    },
  ]);
  const [lastCitations, setLastCitations] = useState<Citation[]>([]);

  const listRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  // Scroll sempre no final
  useEffect(() => {
    if (!listRef.current) return;
    listRef.current.scrollTo({ top: listRef.current.scrollHeight, behavior: 'smooth' });
  }, [messages]);

  async function sendMessage() {
    const text = input.trim();
    if (!text || loading) return;

    setInput('');
    setError(null);
    setLastCitations([]);

    const userMsg: Msg = { role: 'user', content: text };
    const nextMessages = [...messages, userMsg];
    setMessages(nextMessages);

    setLoading(true);
    try {
      // Agora mandamos só { message }; o backend já é híbrido
      const res = await fetch(API_PATH, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text }),
      });

      if (!res.ok) {
        const errTxt = await safeReadText(res);
        throw new Error(errTxt || `HTTP ${res.status}`);
      }

      const data = await res.json();

      // 1) Se vier JSON híbrido, renderiza bonito
      let assistantText = '';
      let citations: Citation[] = [];

      const looksLikeHybrid =
        typeof data === 'object' && (data.summary || data.artifacts || data.sources);
      if (looksLikeHybrid) {
        assistantText = renderHybridToText(data as HybridJSON);
        citations = extractCitations(data as HybridJSON);
      } else {
        // 2) Se o backend devolver { answer } ou { text }, usa texto cru
        assistantText = (data.answer ?? data.output ?? data.text ?? '').trim();
        citations = (data.citations ?? []) as Citation[];
      }

      if (!assistantText) {
        assistantText = '—';
      }

      setLastCitations(citations);
      setMessages((prev) => [...prev, { role: 'assistant', content: assistantText }]);
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Erro desconhecido';
      setError(msg);
    } finally {
      setLoading(false);
      textareaRef.current?.focus();
    }
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      void sendMessage();
    }
  }

  function clearChat() {
    setMessages([
      { role: 'assistant', content: 'Novo chat iniciado. Envie sua User Story ou pergunta.' },
    ]);
    setError(null);
    setLastCitations([]);
    textareaRef.current?.focus();
  }

  return (
    <div className="min-h-screen w-full flex items-center justify-center p-4 bg-gray-100">
      <div className="w-full max-w-3xl bg-white rounded-2xl shadow border p-4">
        <header className="flex items-center justify-between border-b pb-3 mb-4">
          <div>
            <h1 className="text-lg font-semibold text-gray-900">Chat – KB + Criativo (Híbrido)</h1>
            <p className="text-xs text-gray-600">/api/chat → backend Flask /chat (retorna JSON ou texto)</p>
          </div>

          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={clearChat}
              className="px-3 py-1.5 text-sm rounded bg-gray-200 hover:bg-gray-300 text-gray-800"
              aria-label="Limpar conversa"
            >
              Limpar
            </button>
          </div>
        </header>

        <div className="mb-3">
          <span className="inline-flex items-center text-[11px] px-2 py-1 rounded-full bg-indigo-50 text-indigo-700 border border-indigo-200">
            Híbrido: usa KB quando disponível e complementa com criação técnica.
          </span>
        </div>

        <div ref={listRef} className="h-[60vh] overflow-y-auto space-y-3">
          {messages.map((m, i) => (
            <div key={i} className={`flex ${m.role === 'assistant' ? '' : 'justify-end'}`}>
              <div
                className={`max-w-[85%] rounded-2xl px-4 py-3 text-sm whitespace-pre-wrap border text-gray-900 ${
                  m.role === 'assistant' ? 'bg-indigo-50 border-indigo-200' : 'bg-gray-200 border-gray-300'
                }`}
              >
                <div className="text-[11px] uppercase tracking-wider font-semibold text-gray-700 mb-1">
                  {m.role === 'assistant' ? 'Assistente' : 'Você'}
                </div>
                {m.content}
              </div>
            </div>
          ))}

          {!!lastCitations.length && (
            <div className="mt-2 text-xs text-gray-700 bg-gray-50 border border-gray-200 rounded p-3">
              <div className="font-semibold mb-1">Fontes</div>
              <ul className="list-disc pl-5 space-y-1">
                {lastCitations.map((c, i) => (
                  <li key={i}>
                    {c.uri?.startsWith('http') ? (
                      <a className="text-blue-600 underline" href={c.uri} target="_blank">
                        {c.uri}
                      </a>
                    ) : (
                      <code>{c.uri}</code>
                    )}
                    {typeof c.score === 'number' ? ` (score ${c.score.toFixed(3)})` : ''}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {error && (
            <div className="text-red-700 text-sm bg-red-100 border border-red-300 rounded p-3">{error}</div>
          )}
        </div>

        <div className="border-t mt-4 pt-3">
          <div className="flex items-end gap-2">
            <textarea
              ref={textareaRef}
              className="flex-1 rounded border p-3 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-300 text-gray-900"
              placeholder={loading ? 'Enviando…' : 'Escreva e pressione Enter'}
              rows={2}
              value={input}
              disabled={loading}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              aria-label="Mensagem"
            />
            <button
              type="button"
              onClick={sendMessage}
              disabled={loading || !input.trim()}
              className="rounded px-4 py-3 bg-indigo-600 text-white text-sm font-medium disabled:opacity-50 hover:bg-indigo-700"
            >
              {loading ? 'Enviando' : 'Enviar'}
            </button>
          </div>
          <p className="text-[11px] text-gray-600 mt-2">Dica: Shift+Enter quebra linha; Enter envia.</p>
        </div>
      </div>
    </div>
  );
}
