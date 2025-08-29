'use client';
import { useState } from "react";

type Source = { title: string; url?: string | null };
type TestCase = {
  id: string; title: string; type: "functional"|"negative"|"nonfunctional";
  steps: string[]; expected_result: string; tags: string[]; traceability: string[];
};
type ResponseJSON = {
  summary: string;
  artifacts: {
    test_cases: TestCase[];
    acceptance_criteria: string[];
    validation_checklist: string[];
    risks: string[];
    open_questions: string[];
  };
  sources: Source[];
};

export default function App() {
  const [message, setMessage] = useState("");
  const [resp, setResp] = useState<ResponseJSON | null>(null);

  const send = async () => {
    const res = await fetch("http://localhost:8081/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });
    const json = await res.json();
    setResp(json);
  };

  return (
    <div className="p-4 space-y-4">
      <textarea className="w-full border p-2" rows={5}
        placeholder="Cole a User Story aqui e peça casos de teste…"
        value={message} onChange={e=>setMessage(e.target.value)} />
      <button onClick={send} className="border px-4 py-2 rounded">Gerar</button>

      {resp && (
        <div className="space-y-4">
          <section>
            <h2 className="text-xl font-semibold">Resumo</h2>
            <p className="whitespace-pre-wrap">{resp.summary}</p>
          </section>

          <section>
            <h2 className="text-xl font-semibold">Casos de Teste</h2>
            <div className="space-y-3">
              {resp.artifacts?.test_cases?.map(tc => (
                <div key={tc.id} className="border p-3 rounded">
                  <div className="font-medium">{tc.id} — {tc.title}</div>
                  <div className="text-sm opacity-70">Tipo: {tc.type}</div>
                  <div className="mt-2">
                    <div className="font-medium">Passos</div>
                    <ol className="list-decimal ml-5">
                      {tc.steps.map((s,i)=><li key={i}>{s}</li>)}
                    </ol>
                  </div>
                  <div className="mt-2"><span className="font-medium">Resultado Esperado:</span> {tc.expected_result}</div>
                  <div className="mt-1 text-sm">Tags: {tc.tags?.join(", ")}</div>
                  <div className="mt-1 text-sm">Rastreabilidade: {tc.traceability?.join(", ")}</div>
                </div>
              ))}
            </div>
          </section>

          <section>
            <h2 className="text-xl font-semibold">Critérios de Aceitação</h2>
            <ul className="list-disc ml-5">{resp.artifacts?.acceptance_criteria?.map((ac,i)=><li key={i}>{ac}</li>)}</ul>
          </section>

          <section>
            <h2 className="text-xl font-semibold">Checklist de Validação</h2>
            <ul className="list-disc ml-5">{resp.artifacts?.validation_checklist?.map((it,i)=><li key={i}>{it}</li>)}</ul>
          </section>

          <section className="grid gap-4 md:grid-cols-2">
            <div>
              <h2 className="text-xl font-semibold">Riscos</h2>
              <ul className="list-disc ml-5">{resp.artifacts?.risks?.map((r,i)=><li key={i}>{r}</li>)}</ul>
            </div>
            <div>
              <h2 className="text-xl font-semibold">Perguntas em Aberto</h2>
              <ul className="list-disc ml-5">{resp.artifacts?.open_questions?.map((q,i)=><li key={i}>{q}</li>)}</ul>
            </div>
          </section>

          <section>
            <h2 className="text-xl font-semibold">Fontes</h2>
            <ul className="list-disc ml-5">
              {resp.sources?.map((s, i) => (
                <li key={i}>
                  {s.url ? <a href={s.url} className="text-blue-600 underline" target="_blank">{s.title}</a> : s.title}
                </li>
              ))}
            </ul>
          </section>
        </div>
      )}
    </div>
  );
}
