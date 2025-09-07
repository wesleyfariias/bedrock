'use client';

import React, { useEffect, useRef, useState } from 'react';
import type { FC } from 'react';

// ------------------------------------------------------------
// Tipos
// ------------------------------------------------------------
export type Msg = { role: 'user' | 'assistant'; content: string };

type ChatResponse = {
  answer?: string;
  kendra_sources?: string[];
  saved?: string | null;
};

type GenPreview = {
  preview: string;
  kendra_sources: string[];
  notice: string;
};

type GenSaved = {
  saved: string; // s3 uri
  meta?: Record<string, unknown>;
};

// Mensagem estendida para suportar "prévia + aprovar" dentro do chat
type UiMsg = {
  role: 'user' | 'assistant';
  content: string;
  // Campos de prévia quando for um gerador
  previewKind?: 'story' | 'rtr';
  previewBody?: string;
  kendra_sources?: string[];
  approvePayload?: any;
  savedUri?: string | null;
  error?: string | null;
};

// ------------------------------------------------------------
// Rotas (Next.js deve fazer rewrite de /api/* -> seu backend FastAPI)
// ------------------------------------------------------------
const API = {
  chat: '/api/chat',
  story: '/api/gen/user-story',
  rtr: '/api/gen/rtr',
};

// ------------------------------------------------------------
// Utils
// ------------------------------------------------------------
function scrollToBottom(ref: React.RefObject<HTMLDivElement>) {
  if (!ref.current) return;
  ref.current.scrollTo({ top: ref.current.scrollHeight, behavior: 'smooth' });
}

async function safeReadText(res: Response) {
  try {
    return await res.text();
  } catch {
    return res.statusText || 'Erro HTTP';
  }
}

function cleanMultiline(s: string) {
  return s.replace(/\n{3,}/g, '\n\n');
}

function parseCommand(input: string):
  | { kind: 'story' | 'rtr'; objetivo: string; contexto: string | null }
  | null {
  const text = input.trim();
  const normalize = (s: string) => s.replace(/\s+/g, ' ').trim();

  const tryParse = (cmd: 'story' | 'rtr') => {
    if (!text.toLowerCase().startsWith(`/${cmd}`)) return null;
    const rest = normalize(text.slice(cmd.length + 1)); // remove "/cmd "
    if (!rest) return { kind: cmd, objetivo: '', contexto: null };
    const [objetivoRaw, ctxRaw] = rest.split(/\|\s*ctx\s*:/i);
    return {
      kind: cmd,
      objetivo: normalize(objetivoRaw || ''),
      contexto: ctxRaw ? normalize(ctxRaw) : null,
    };
  };

  return tryParse('story') || tryParse('rtr');
}

// ------------------------------------------------------------
// Componente principal (chat único com comandos)
// ------------------------------------------------------------
const BedrockClaudeChatUnified: FC = () => {
  const [messages, setMessages] = useState<UiMsg[]>([
    {
      role: 'assistant',
      content:
        'Olá! Eu consigo conversar normalmente e também gerar artefatos via comandos. Exemplos:\n' +
        '• /story <objetivo> | ctx: <contexto opcional>\n' +
        '• /rtr <objetivo> | ctx: <contexto opcional>\n' +
        'Depois que eu mostrar a prévia, clique em “Aprovar & Salvar no S3”.',
    },
  ]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const chatTextareaRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => scrollToBottom(listRef), [messages]);

  // ---------------- Core: enviar ----------------
  async function onSend() {
    const text = input.trim();
    if (!text || loading) return;

    setInput('');
    setError(null);

    // Adiciona a mensagem do usuário imediatamente
    const userMsg: UiMsg = { role: 'user', content: text };
    setMessages((prev) => [...prev, userMsg]);

    // 1) Detecta e trata comandos (geradores)
    const cmd = parseCommand(text);
    if (cmd) {
      const { kind, objetivo, contexto } = cmd;

      if (!objetivo) {
        // resposta de ajuda
        const hint =
          kind === 'story'
            ? 'Uso: /story <objetivo> | ctx: <contexto opcional>'
            : 'Uso: /rtr <objetivo> | ctx: <contexto opcional>';
        setMessages((prev) => [...prev, { role: 'assistant', content: hint }]);
        return;
      }

      setLoading(true);
      try {
        const api = kind === 'story' ? API.story : API.rtr;
        const payload = { objetivo, contexto: contexto || null, approve: false };
        const res = await fetch(api, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        if (!res.ok) {
          const errTxt = await safeReadText(res);
          throw new Error(errTxt || `HTTP ${res.status}`);
        }
        const data = (await res.json()) as GenPreview;
        const previewBody = cleanMultiline(data.preview || '');
        const previewMsg: UiMsg = {
          role: 'assistant',
          content: '',
          previewKind: kind,
          previewBody,
          kendra_sources: data.kendra_sources,
          approvePayload: { ...payload, approve: true },
        };
        setMessages((prev) => [...prev, previewMsg]);
      } catch (e) {
        const msg = e instanceof Error ? e.message : 'Erro desconhecido';
        setMessages((prev) => [
          ...prev,
          { role: 'assistant', content: 'Falha ao gerar prévia.', error: msg },
        ]);
      } finally {
        setLoading(false);
      }
      return;
    }

    // 2) Caso contrário, é chat normal
    setLoading(true);
    try {
      const history: Msg[] = messages.map(({ role, content }) => ({ role, content }));
      const res = await fetch(API.chat, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages: [...history, { role: 'user', content: text }] }),
      });
      if (!res.ok) {
        const errTxt = await safeReadText(res);
        throw new Error(errTxt || `HTTP ${res.status}`);
      }
      const data = (await res.json()) as ChatResponse;
      const answer = cleanMultiline((data.answer || '').trim());
      setMessages((prev) => [...prev, { role: 'assistant', content: answer || '—' }]);
      // opcional: você pode renderizar fontes do Kendra como outra mensagem, se quiser
      if (data.kendra_sources?.length) {
        setMessages((prev) => [
          ...prev,
          { role: 'assistant', content: 'Fontes:' },
          { role: 'assistant', content: (data.kendra_sources || []).map((s) => `• ${s}`).join('\n') },
        ]);
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Erro desconhecido';
      setError(msg);
      setMessages((prev) => [...prev, { role: 'assistant', content: 'Erro ao responder.' }]);
    } finally {
      setLoading(false);
      chatTextareaRef.current?.focus();
    }
  }

  function onKey(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      void onSend();
    }
  }

  async function onApprove(msg: UiMsg) {
    if (!msg.previewKind || !msg.approvePayload) return;
    const api = msg.previewKind === 'story' ? API.story : API.rtr;
    try {
      const res = await fetch(api, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(msg.approvePayload),
      });
      if (!res.ok) {
        const errTxt = await safeReadText(res);
        throw new Error(errTxt || `HTTP ${res.status}`);
      }
      const data = (await res.json()) as GenSaved;
      setMessages((prev) => prev.map((m) => (m === msg ? { ...m, savedUri: data.saved } : m)));
    } catch (e) {
      const err = e instanceof Error ? e.message : 'Erro ao salvar';
      setMessages((prev) => prev.map((m) => (m === msg ? { ...m, error: err } : m)));
    }
  }

  function clearChat() {
    setMessages([
      {
        role: 'assistant',
        content:
          'Novo chat iniciado. Comandos disponíveis:\n' +
          '• /story <objetivo> | ctx: <contexto opcional>\n' +
          '• /rtr <objetivo> | ctx: <contexto opcional>',
      },
    ]);
    setError(null);
  }

  // ------------------------------------------------------------
  // Render
  // ------------------------------------------------------------
  return (
    <div className="min-h-screen w-full flex items-center justify-center p-4 bg-gray-100">
      <div className="w-full max-w-4xl bg-white rounded-2xl shadow border p-4">
        <header className="flex items-center justify-between border-b pb-3 mb-4">
          <div>
            <h1 className="text-lg font-semibold text-gray-900">Assistente PMESP – Chat único</h1>
            <p className="text-xs text-gray-600">Converse normalmente ou use comandos para gerar artefatos (prévia ➜ aprovar ➜ salvar no S3).</p>
          </div>
          <button
            type="button"
            onClick={clearChat}
            className="px-3 py-1.5 text-sm rounded bg-gray-200 hover:bg-gray-300 text-gray-800"
          >
            Limpar
          </button>
        </header>

        <section>
          <div className="mb-3">
            <span className="inline-flex items-center text-[11px] px-2 py-1 rounded-full bg-indigo-50 text-indigo-700 border border-indigo-200">
              /api/chat para conversa • /api/gen/user-story e /api/gen/rtr para comandos
            </span>
          </div>

          <div ref={listRef} className="h-[65vh] overflow-y-auto space-y-3">
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

                  {m.previewKind ? (
                    <div>
                      <div className="text-[11px] font-semibold mb-2">Prévia {m.previewKind.toUpperCase()}</div>
                      <div className="rounded border bg-white p-3 whitespace-pre-wrap text-sm text-gray-900">
                        {m.previewBody}
                      </div>

                      {!!(m.kendra_sources?.length) && (
                        <div className="mt-2 text-xs text-gray-700 bg-gray-50 border border-gray-200 rounded p-3">
                          <div className="font-semibold mb-1">Fontes</div>
                          <ul className="list-disc pl-5 space-y-1">
                            {m.kendra_sources!.map((s, idx) => (
                              <li key={idx}>
                                {s.startsWith('http') ? (
                                  <a className="text-blue-600 underline" href={s} target="_blank">
                                    {s}
                                  </a>
                                ) : (
                                  <code>{s}</code>
                                )}
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}

                      <div className="mt-2 flex gap-2">
                        <button
                          className="px-3 py-1.5 text-sm rounded bg-indigo-600 text-white disabled:opacity-50 hover:bg-indigo-700"
                          onClick={() => onApprove(m)}
                          disabled={!!m.savedUri}
                        >
                          Aprovar & Salvar no S3
                        </button>
                        {m.savedUri && (
                          <span className="text-xs text-gray-700">Salvo em: <code>{m.savedUri}</code></span>
                        )}
                        {m.error && (
                          <span className="text-xs text-red-700">{m.error}</span>
                        )}
                      </div>
                    </div>
                  ) : (
                    <div>{m.content}</div>
                  )}
                </div>
              </div>
            ))}

            {error && (
              <div className="text-red-700 text-sm bg-red-100 border border-red-300 rounded p-3">{error}</div>
            )}
          </div>

          <div className="border-t mt-4 pt-3">
            <div className="flex items-end gap-2">
              <textarea
                ref={chatTextareaRef}
                className="flex-1 rounded border p-3 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-300 text-gray-900"
                placeholder={loading ? 'Enviando…' : 'Digite aqui. Exemplos: /story … | ctx: …  •  /rtr … | ctx: …'}
                rows={2}
                value={input}
                disabled={loading}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={onKey}
                aria-label="Mensagem"
              />
              <button
                type="button"
                onClick={onSend}
                disabled={loading || !input.trim()}
                className="rounded px-4 py-3 bg-indigo-600 text-white text-sm font-medium disabled:opacity-50 hover:bg-indigo-700"
              >
                {loading ? 'Enviando' : 'Enviar'}
              </button>
            </div>
            <p className="text-[11px] text-gray-600 mt-2">Dica: Shift+Enter quebra linha; Enter envia.</p>
          </div>
        </section>
      </div>
    </div>
  );
};

export default BedrockClaudeChatUnified;
