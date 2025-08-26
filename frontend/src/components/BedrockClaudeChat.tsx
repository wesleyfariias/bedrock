'use client';

import React, { useEffect, useRef, useState } from 'react'

// Tipos
export type Msg = { role: 'user' | 'assistant'; content: string }
type Citation = { uri: string; score?: number | null }

// Caminho da API (bate no rewrite do next.config.ts → Flask /chat)
const API_PATH = '/api/chat'

export default function BedrockClaudeChat() {
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [messages, setMessages] = useState<Msg[]>([
    { role: 'assistant', content: 'Olá! Sou o Bedrock. Como posso te ajudar hoje?' },
  ])
  const [lastCitations, setLastCitations] = useState<Citation[]>([])
  const [origin, setOrigin] = useState<string | null>(null) // X-Service do backend, se vier

  const listRef = useRef<HTMLDivElement | null>(null)
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)

  // Foco ao montar
  useEffect(() => {
    textareaRef.current?.focus()
  }, [])

  // Scroll automático
  useEffect(() => {
    if (listRef.current) {
      listRef.current.scrollTo({ top: listRef.current.scrollHeight, behavior: 'smooth' })
    }
  }, [messages])

  async function sendMessage() {
    const text = input.trim()
    if (!text || loading) return

    setInput('')
    setError(null)
    setLastCitations([])

    const userMsg: Msg = { role: 'user', content: text }
    const nextMessages = [...messages, userMsg]
    setMessages(nextMessages)

    setLoading(true)
    try {
      const res = await fetch(API_PATH, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        // o backend só usa "message"; "history" é opcional
        body: JSON.stringify({ message: text, history: nextMessages }),
      })

      // registra cabeçalho de origem se o proxy repassar (X-Service: flask-kb)
      setOrigin(res.headers.get('x-service'))

      if (!res.ok) {
        const errTxt = await safeReadText(res)
        throw new Error(errTxt || `HTTP ${res.status}`)
      }

      // Backend retorna { answer, citations } (e às vezes { error, detail })
      const data = await res.json() as {
        answer?: string
        output?: string
        citations?: Citation[]
        error?: string
        detail?: string
      }

      if (data.error || data.detail) throw new Error(data.error || data.detail)

      const reply = (data.answer ?? data.output ?? '').trim()
      setLastCitations(data.citations ?? [])
      setMessages((prev) => [...prev, { role: 'assistant', content: reply || '—' }])
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Erro desconhecido'
      setError(msg)
    } finally {
      setLoading(false)
      textareaRef.current?.focus()
    }
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      void sendMessage()
    }
  }

  function clearChat() {
    setMessages([{ role: 'assistant', content: 'Novo chat iniciado. O que você quer fazer?' }])
    setError(null)
    setLastCitations([])
    setOrigin(null)
    textareaRef.current?.focus()
  }

  return (
    <div className="min-h-screen w-full flex items-center justify-center p-4 bg-gray-100">
      <div className="w-full max-w-3xl bg-white rounded-2xl shadow border p-4">
        <header className="flex items-center justify-between border-b pb-3 mb-4">
          <div>
            <h1 className="text-lg font-semibold text-gray-900">Chat – Bedrock (KB)</h1>
            <p className="text-xs text-gray-600">
              Proxy: <code>/api/chat</code> → Flask <code>/chat</code>
              {origin ? <> • origem: <code>{origin}</code></> : null}
            </p>
          </div>
          <button
            type="button"
            onClick={clearChat}
            className="px-3 py-1.5 text-sm rounded bg-gray-200 hover:bg-gray-300 text-gray-800"
            aria-label="Limpar conversa"
          >
            Limpar
          </button>
        </header>

        <div ref={listRef} className="h-[60vh] overflow-y-auto space-y-3">
          {messages.map((m, i) => (
            <div key={i} className={`flex ${m.role === 'assistant' ? '' : 'justify-end'}`}>
              <div
                className={`max-w-[85%] rounded-2xl px-4 py-3 text-sm whitespace-pre-wrap border text-gray-900 ${
                  m.role === 'assistant'
                    ? 'bg-indigo-50 border-indigo-200'
                    : 'bg-gray-200 border-gray-300'
                }`}
              >
                <div className="text-[11px] uppercase tracking-wider font-semibold text-gray-700 mb-1">
                  {m.role === 'assistant' ? 'Bedrock' : 'Você'}
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
                    <code>{c.uri}</code>
                    {typeof c.score === 'number' ? ` (score ${c.score.toFixed(3)})` : ''}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {error && (
            <div className="text-red-700 text-sm bg-red-100 border border-red-300 rounded p-3">
              {error}
            </div>
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
  )
}

// Lê texto de erro do Response sem quebrar se não for body-text
async function safeReadText(res: Response) {
  try { return await res.text() } catch { return res.statusText || 'Erro HTTP' }
}
