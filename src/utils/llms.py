import asyncio
from typing import List

from loguru import logger
from google import genai
from google.genai import types

from src.config import env


class GeminiClient:
    """
    Cliente para a API Gemini do Google.
    """

    def __init__(self):
        self.client = genai.Client(
            api_key=env.GEMINI_API_KEY,
        )

    async def generate_content(
        self,
        prompt: str,
        model_name: str = "gemini-2.5-flash",
        temperature: float = 0.7,
    ) -> str:
        try:
            generate_content_config = types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(
                    thinking_budget=-1,
                ),
                temperature=temperature,
            )
            response = await self.client.aio.models.generate_content(
                model=model_name,
                contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
                config=generate_content_config,
            )
            if response.text:
                return response.text
            else:
                logger.error("Resposta  está vazia.")
                raise BaseException("No text response received from Gemini AI.")
        except Exception as e:
            logger.error(f"Erro ao executar o prompt : {e}", exc_info=True)
            raise

    async def generate_embedding(self, chunks: List[str]) -> List:
        try:

            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self.client.models.embed_content(
                    model="text-embedding-004",
                    contents=chunks,
                ),
            )

            embeddings = [embedding.values for embedding in response.embeddings]
            logger.info(
                f"Generated {len(embeddings)} embeddings for {len(chunks)} chunks."
            )
            return embeddings
        except Exception as e:
            logger.error(f"Erro ao gerar embedding de forma assíncrona: {e}")
            return [[0.0] * 768 for _ in chunks]  # Retorna embeddings vazios
