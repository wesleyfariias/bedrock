export const runtime = "nodejs";
export const dynamic = "force-dynamic";

import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";

type Msg = { role: "user" | "assistant"; content: string };

function toAnthropicPrompt(history: Msg[] = [], userMessage: string) {
	  const lines: string[] = [];
	    for (const m of history) {
		        if (m.role === "user") {
				      lines.push("\n\nHuman: " + m.content);
				          } else {
						        lines.push("\n\nAssistant: " + m.content);
							    }
							      }
							        lines.push("\n\nHuman: " + userMessage);
								  lines.push("\n\nAssistant:");
								    const prompt = lines.join("");
								      return prompt.startsWith("\n\nHuman:") ? prompt : "\n\nHuman:" + prompt;
}

export async function POST(req: NextRequest) {
	  try {
		      const { message, history } = await req.json();
		          if (!message) {
				        return NextResponse.json({ error: "message é obrigatório" }, { status: 400 });
					    }

					        const { BedrockRuntimeClient, InvokeModelCommand } = await import("@aws-sdk/client-bedrock-runtime");

						    const client = new BedrockRuntimeClient({
							          region: process.env.AWS_REGION || "us-east-1",
								      });

								          const payload = {
										        prompt: toAnthropicPrompt(history as Msg[] | undefined, String(message)),
											      max_tokens_to_sample: 400,
											            temperature: 0.7,
												          stop_sequences: ["\n\nHuman:"],
													      };

													          const cmd = new InvokeModelCommand({
															        modelId: "anthropic.claude-v2",
																      contentType: "application/json",
																            accept: "application/json",
																	          body: Buffer.from(JSON.stringify(payload)),
																		      });

																		          const resp = await client.send(cmd);
																			      const jsonText = Buffer.from((resp as any).body || new Uint8Array()).toString("utf-8");
																			          const data = JSON.parse(jsonText);
																				      const output = data.completion || data.output || "";

																				          return NextResponse.json({ output });
																					    } catch (e) {
																						        const err = e as Error;
																							    console.error("Erro ao chamar Bedrock:", err);
																							        return NextResponse.json({ error: err.message || "Erro interno" }, { status: 500 });
																								  }
}

