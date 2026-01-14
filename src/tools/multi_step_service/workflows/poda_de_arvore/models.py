"""
Modelos Pydantic para validação do workflow de Poda de Árvore
"""

import re
from typing import Literal, Optional, Union
from pydantic import BaseModel, Field, field_validator


class NomePayload(BaseModel):
    name: Optional[str] = Field(
        None, 
        min_length=3, 
        max_length=100,
        description="Nome e sobrenome do usuário (opcional)"
    )
    
    @field_validator('name', mode='before')
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        """
        Valida e formata o nome do usuário.
        - Remove espaços extras
        - Verifica nome e sobrenome
        - Aceita apenas letras e acentos
        """
        if not v or not v.strip():
            return None
            
        # Remove espaços extras
        v = ' '.join(v.split())
        
        # Verifica se tem pelo menos nome e sobrenome
        if len(v.split()) < 2:
            raise ValueError("Por favor, informe nome e sobrenome")
        
        # Verifica se contém apenas letras, espaços e caracteres válidos
        if not re.match(r"^[a-zA-ZÀ-ÿ\s'-]+$", v):
            raise ValueError("Nome deve conter apenas letras")
        
        if any(len(element) < 2 for element in v.split()) :
            raise ValueError("Cada parte do nome deve ter pelo menos 2 caracteres")

        # Capitaliza cada palavra
        v = ' '.join(word.capitalize() for word in v.split())
        
        return v

class EmailPayload(BaseModel):
    email: Optional[str] = Field(
        None,
        description="Email válido do usuário (opcional)"
    )
    
    @field_validator('email', mode='before')
    @classmethod
    def validate_email(cls, v: Optional[str]) -> Optional[str]:
        """
        Valida e normaliza email.
        """
        if not v or not v.strip():
            return None
            
        v = v.strip().lower()
        
        # Validação de formato de email
        email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        if not re.match(email_pattern, v):
            raise ValueError("Email inválido. Use o formato: exemplo@dominio.com")
        
        return v

class CPFPayload(BaseModel):
    cpf: Optional[str] = Field(
        None,
        description="CPF com 11 dígitos (deixe vazio se o usuário não quiser se identificar)"
    )

    @field_validator('cpf', mode='before')
    @classmethod
    def validate_cpf(cls, v: Optional[str]) -> Optional[str]:
        """
        Valida CPF com verificação de dígitos.
        Remove formatação e valida algoritmo.
        Retorna None se vazio (usuário não quer se identificar).
        """
        # Se vazio ou None, usuário não quer se identificar
        if not v:
            return None
            
        # Remove qualquer caractere não numérico
        cpf = re.sub(r'\D', '', str(v))
        
        if len(cpf) != 11 or len(set(cpf)) == 1:
            raise ValueError("CPF inválido")
        
        # Validação do primeiro dígito verificador
        soma = sum(int(cpf[i]) * (10 - i) for i in range(9))
        digito1 = (soma * 10 % 11) % 10
        
        if int(cpf[9]) != digito1:
            raise ValueError("CPF inválido - dígito verificador incorreto")
        
        # Validação do segundo dígito verificador
        soma = sum(int(cpf[i]) * (11 - i) for i in range(10))
        digito2 = (soma * 10 % 11) % 10
        
        if int(cpf[10]) != digito2:
            raise ValueError("CPF inválido - dígito verificador incorreto")
        
        return cpf


class AddressData(BaseModel):
    """
    Modelo para dados de endereço processados.
    Armazena informações completas do endereço.
    """
    
    # Dados básicos do endereço
    logradouro: str = Field(
        ...,
        description="Rua, avenida, etc"
    )
    numero: Optional[str] = Field(
        "",
        description="Número do endereço"
    )
    complemento: Optional[str] = Field(
        None,
        description="Apartamento, bloco, etc (opcional)"
    )
    ponto_referencia: Optional[str] = Field(
        None,
        description="Ponto de referência (opcional)"
    )
    bairro: str = Field(
        ...,
        description="Bairro"
    )
    cep: Optional[str] = Field(
        None,
        description="CEP do endereço"
    )
    cidade: str = Field(
        default="Rio de Janeiro",
        description="Cidade"
    )
    estado: str = Field(
        default="RJ",
        description="Estado (sigla)"
    )
    
    # Dados de geolocalização
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    
    # Dados do IPP (quando disponíveis)
    logradouro_id_ipp: Optional[Union[int, str]] = None
    logradouro_nome_ipp: Optional[str] = None
    bairro_id_ipp: Optional[Union[int, str]] = None
    bairro_nome_ipp: Optional[str] = None
    
    # Endereço formatado
    formatted_address: Optional[str] = None
    
    # Texto original fornecido pelo usuário
    original_text: Optional[str] = None
    
    @field_validator('cep', mode='before')
    @classmethod
    def validar_cep(cls, v: Optional[str]) -> Optional[str]:
        """Valida e formata CEP."""
        if not v:
            return None
        # Remove tudo que não é número
        cep = re.sub(r'\D', '', str(v))
        
        if len(cep) != 8:
            return None  # Retorna None se inválido em vez de lançar erro
        
        return cep


class AddressPayload(BaseModel):
    """Payload para coleta de endereço."""
    address: str = Field(
        ...,
        description="Endereço completo"
    )


class AddressConfirmationPayload(BaseModel):
    """Payload para confirmação de endereço."""
    confirmacao: bool = Field(
        ..., 
        description="Confirmação se os dados estão corretos"
    )


class AddressValidationState(BaseModel):
    """Estado da validação de endereço."""
    attempts: int = 0
    max_attempts: int = 3
    last_error: Optional[str] = None
    validated: bool = False


class PontoReferenciaPayload(BaseModel):
    """Payload para coleta de ponto de referência."""
    ponto_referencia: Optional[str] = Field(
        None,
        description="Ponto de referência próximo ao local (opcional - deixe vazio se não quiser informar)"
    )


class TicketDataConfirmationPayload(BaseModel):
    """Payload para confirmação ou correção dos dados do ticket."""
    confirmacao: Optional[bool] = Field(
        None, 
        description="True se os dados estão corretos, False se precisam de correção"
    )
    correcao: Optional[str] = Field(
        None,
        description="Descrição do que precisa ser corrigido (quando confirmacao=False)"
    )
    
    @field_validator('correcao', mode='after')
    @classmethod
    def validate_correcao(cls, v: Optional[str], info) -> Optional[str]:
        """Valida que há uma correção quando confirmacao é False."""
        values = info.data
        return v
