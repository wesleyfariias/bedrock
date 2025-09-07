'use client';

import React, { useEffect, useRef, useState } from 'react';

// Tipos compartilhados
export type Msg = { role: 'user' | 'assistant'; content: string };

type Citation = { uri: string; score?: number | null };

type ChatMsg = Msg;

type ChatResponse = {
  answer?: string;
  kendra_sources?: string[];
  saved?: string | null;
};

type GenStoryPreview = {
  preview: string;
  kendra_sources: string[];
  notice: string;
};

type GenStorySaved = {
  saved: string; // s3 uri
  meta?: Record<string, unknown>;
};

// Rotas (Next.js deve fazer rewrite de /api/* -> seu backend FastAPI)
const API = {
  chat: '/api/chat',
  story: '/api/gen/user-story',
  pipeline: '/api/gen/pipeline', // preparado p/ futuro
  testcases: '/api/gen/testcases', // preparado p/ futuro
  ptr: '/api/gen/ptr', // preparado p/ futuro
};

// Utils
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

function asCitations(arr?: string[]): Citation[] {
  return (arr || []).map((u) => ({ uri: u }));
}

// ------------------------------------------------------------
// Componente principal
// ------------------------------------------------------------
export default function BedrockClaudeChatRefactor() {
  // UI state
  const [tab, setTab] = useState<'chat' | 'story'>('chat'); // tabs ativas (expansível)

  // Chat state
  const [messages, setMessages] = useState<Msg[]>([
    {
      role: 'assistant',
      content:
        'Olá! Este front usa o backend FastAPI: /chat (chat com Kendra) e /gen/user-story (prévia ➜ aprovar ➜ salvar no S3).',
    },
  ]);
  const [input, setInput] = useState('');
  const [chatLoading, setChatLoading] = useState(false);
  const [chatError, setChatError] = useState<string | null>(null);
  const [chatCitations, setChatCitations] = useState<Citation[]>([]);

  // Story state
  const [storyObj, setStoryObj] = useState('');
  const [storyCtx, setStoryCtx] = useState('');
  const [storyLoading, setStoryLoading] = useState(false);
  const [storyError, setStoryError] = useState<string | null>(null);
  const [storyPreview, setStoryPreview] = useState<string>('');
  const [storySavedUri, setStorySavedUri] = useState<string | null>(null);
  const [storyCitations, setStoryCitations] = useState<Citation[]>([]);

  const listRef = useRef<HTMLDivElement | null>(null);
  const chatTextareaRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => scrollToBottom(listRef), [messages]);

  // ---------------- CHAT ----------------
  async function onSendChat() {
    const text = input.trim();
    if (!text || chatLoading) return;

    setInput('');
    setChatError(null);
    setChatCitations([]);

    const userMsg: Msg = { role: 'user', content: text };
    const nextMessages = [...messages, userMsg];
    setMessages(nextMessages);

    setChatLoading(true);
    try {
      // Novo backend /chat espera { messages: [{role, content}, ...] }
      const res = await fetch(API.chat, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages: nextMessages.map(({ role, content }) => ({ role, content })) }),
      });

      if (!res.ok) {
        const errTxt = await safeReadText(res);
        throw new Error(errTxt || `HTTP ${res.status}`);
      }

      const data = (await res.json()) as ChatResponse;
      const answer = cleanMultiline((data.answer || '').trim());
      const citations = asCitations(data.kendra_sources);

      setChatCitations(citations);
      setMessages((prev) => [...prev, { role: 'assistant', content: answer || '—' }]);
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Erro desconhecido';
      setChatError(msg);
    } finally {
      setChatLoading(false);
      chatTextareaRef.current?.focus();
    }
  }

  function onChatKey(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      void onSendChat();
    }
  }

  function clearChat() {
    setMessages([
      { role: 'assistant', content: 'Novo chat iniciado. Faça sua pergunta técnica.' },
    ]);
    setChatError(null);
    setChatCitations([]);
  }

  // ---------------- USER STORY ----------------
  async function onStoryPreview() {
    if (!storyObj.trim()) {
      setStoryError('Informe o objetivo.');
      return;
    }
    setStoryError(null);
    setStoryPreview('');
    setStorySavedUri(null);
    setStoryCitations([]);
    setStoryLoading(true);

    try {
      const res = await fetch(API.story, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ objetivo: storyObj, contexto: storyCtx || null, approve: false }),
      });
      if (!res.ok) {
        const errTxt = await safeReadText(res);
        throw new Error(errTxt || `HTTP ${res.status}`);
      }
      const data = (await res.json()) as GenStoryPreview;
      setStoryPreview(cleanMultiline(data.preview || ''));
      setStoryCitations(asCitations(data.kendra_sources));
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Erro desconhecido';
      setStoryError(msg);
    } finally {
      setStoryLoading(false);
    }
  }

  async function onStoryApprove() {
    if (!storyObj.trim()) return;
    setStoryLoading(true);
    setStoryError(null);
    try {
      const res = await fetch(API.story, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ objetivo: storyObj, contexto: storyCtx || null, approve: true }),
      });
      if (!res.ok) {
        const errTxt = await safeReadText(res);
        throw new Error(errTxt || `HTTP ${res.status}`);
      }
      const data = (await res.json()) as GenStorySaved;
      setStorySavedUri(data.saved || null);
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Erro desconhecido';
      setStoryError(msg);
    } finally {
      setStoryLoading(false);
    }
  }

  function clearStory() {
    setStoryObj('');
    setStoryCtx('');
    setStoryPreview('');
    setStorySavedUri(null);
    setStoryCitations([]);
    setStoryError(null);
  }

  // ------------------------------------------------------------
  // Render
  // ------------------------------------------------------------
  return (
    <div className="min-h-screen w-full flex items-center justify-center p-4 bg-gray-100">
      <div className="w-full max-w-4xl bg-white rounded-2xl shadow border p-4">
        <header className="flex items-center justify-between border-b pb-3 mb-4">
          <div>
            <h1 className="text-lg font-semibold text-gray-900">Assistente PMESP – KB + Geração</h1>
            <p className="text-xs text-gray-600">Chat (Kendra+Claude 2.1) e Geração de User Story com aprovação e salvamento no S3.</p>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setTab('chat')}
              className={`px-3 py-1.5 text-sm rounded border ${
                tab === 'chat' ? 'bg-indigo-600 text-white border-indigo-600' : 'bg-white text-gray-800 border-gray-300'
              }`}
            >
              Chat
            </button>
            <button
              type="button"
              onClick={() => setTab('story')}
              className={`px-3 py-1.5 text-sm rounded border ${
                tab === 'story' ? 'bg-indigo-600 text-white border-indigo-600' : 'bg-white text-gray-800 border-gray-300'
              }`}
            >
              User Story
            </button>
          </div>
        </header>

        {tab === 'chat' ? (
          <section>
            <div className="mb-3">
              <span className="inline-flex items-center text-[11px] px-2 py-1 rounded-full bg-indigo-50 text-indigo-700 border border-indigo-200">
                /api/chat → backend FastAPI /chat (usa Kendra quando disponível)
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

              {!!chatCitations.length && (
                <div className="mt-2 text-xs text-gray-700 bg-gray-50 border border-gray-200 rounded p-3">
                  <div className="font-semibold mb-1">Fontes</div>
                  <ul className="list-disc pl-5 space-y-1">
                    {chatCitations.map((c, i) => (
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

              {chatError && (
                <div className="text-red-700 text-sm bg-red-100 border border-red-300 rounded p-3">{chatError}</div>
              )}
            </div>

            <div className="border-t mt-4 pt-3">
              <div className="flex items-end gap-2">
                <textarea
                  ref={chatTextareaRef}
                  className="flex-1 rounded border p-3 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-300 text-gray-900"
                  placeholder={chatLoading ? 'Enviando…' : 'Escreva e pressione Enter'}
                  rows={2}
                  value={input}
                  disabled={chatLoading}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={onChatKey}
                  aria-label="Mensagem"
                />
                <button
                  type="button"
                  onClick={onSendChat}
                  disabled={chatLoading || !input.trim()}
                  className="rounded px-4 py-3 bg-indigo-600 text-white text-sm font-medium disabled:opacity-50 hover:bg-indigo-700"
                >
                  {chatLoading ? 'Enviando' : 'Enviar'}
                </button>
                <button
                  type="button"
                  onClick={clearChat}
                  className="px-3 py-3 text-sm rounded bg-gray-200 hover:bg-gray-300 text-gray-800"
                  aria-label="Limpar conversa"
                >
                  Limpar
                </button>
              </div>
              <p className="text-[11px] text-gray-600 mt-2">Dica: Shift+Enter quebra linha; Enter envia.</p>
            </div>
          </section>
        ) : (
          <section>
            <div className="mb-3">
              <span className="inline-flex items-center text-[11px] px-2 py-1 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200">
                /api/gen/user-story → backend FastAPI /gen/user-story (prévia ➜ aprovar ➜ salvar no S3)
              </span>
            </div>

            <div className="grid grid-cols-1 gap-3">
              <label className="block">
                <span className="text-xs text-gray-700">Objetivo (obrigatório)</span>
                <input
                  type="text"
                  value={storyObj}
                  onChange={(e) => setStoryObj(e.target.value)}
                  className="mt-1 w-full rounded border p-2 text-sm focus:outline-none focus:ring-2 focus:ring-emerald-300"
                  placeholder="Ex.: Implementar autenticação OIDC no Portal X"
                />
              </label>

              <label className="block">
                <span className="text-xs text-gray-700">Contexto (opcional)</span>
                <textarea
                  value={storyCtx}
                  onChange={(e) => setStoryCtx(e.target.value)}
                  rows={3}
                  className="mt-1 w-full rounded border p-2 text-sm focus:outline-none focus:ring-2 focus:ring-emerald-300"
                  placeholder="Detalhes adicionais, dependências, squads envolvidas, etc."
                />
              </label>

              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={onStoryPreview}
                  disabled={storyLoading || !storyObj.trim()}
                  className="rounded px-4 py-2 bg-emerald-600 text-white text-sm font-medium disabled:opacity-50 hover:bg-emerald-700"
                >
                  {storyLoading ? 'Gerando…' : 'Gerar Prévia'}
                </button>
                <button
                  type="button"
                  onClick={onStoryApprove}
                  disabled={storyLoading || !storyPreview}
                  className="rounded px-4 py-2 bg-indigo-600 text-white text-sm font-medium disabled:opacity-50 hover:bg-indigo-700"
                >
                  Aprovar & Salvar no S3
                </button>
                <button
                  type="button"
                  onClick={clearStory}
                  className="rounded px-4 py-2 bg-gray-200 text-gray-800 text-sm hover:bg-gray-300"
                >
                  Limpar
                </button>
              </div>

              {storyError && (
                <div className="text-red-700 text-sm bg-red-100 border border-red-300 rounded p-3">{storyError}</div>
              )}

              {!!storyPreview && (
                <div className="mt-2">
                  <div className="text-xs font-semibold text-gray-700 mb-1">Prévia</div>
                  <div className="rounded border bg-gray-50 p-3 whitespace-pre-wrap text-sm text-gray-900">
                    {storyPreview}
                  </div>
                </div>
              )}

              {!!storyCitations.length && (
                <div className="mt-2 text-xs text-gray-700 bg-gray-50 border border-gray-200 rounded p-3">
                  <div className="font-semibold mb-1">Fontes</div>
                  <ul className="list-disc pl-5 space-y-1">
                    {storyCitations.map((c, i) => (
                      <li key={i}>
                        {c.uri?.startsWith('http') ? (
                          <a className="text-blue-600 underline" href={c.uri} target="_blank">
                            {c.uri}
                          </a>
                        ) : (
                          <code>{c.uri}</code>
                        )}
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {storySavedUri && (
                <div className="mt-2 text-xs text-gray-700 bg-emerald-50 border border-emerald-200 rounded p-3">
                  <div className="font-semibold mb-1">Salvo</div>
                  <div>
                    Artefato salvo em: <code>{storySavedUri}</code>
                  </div>
                </div>
              )}
            </div>
          </section>
        )}
      </div>
    </div>
  );
}
