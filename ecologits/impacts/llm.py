import math
from math import ceil
from typing import Any, Optional, Union, cast

from ecologits._ecologits import EcoLogits
from ecologits.impacts.dag import DAG
from ecologits.impacts.modeling import GWP, PE, ADPe, Embodied, Energy, Impacts, Usage, Water
from ecologits.utils.range_value import RangeValue, ValueOrRange

MODEL_QUANTIZATION_BITS = 4

GPU_ENERGY_ALPHA = 8.91e-8
GPU_ENERGY_BETA = 1.43e-6
GPU_ENERGY_STDEV = 5.19e-7
GPU_LATENCY_ALPHA = 8.02e-4
GPU_LATENCY_BETA = 2.23e-2
GPU_LATENCY_STDEV = 7.00e-6

GPU_MEMORY = 80  # GB
GPU_EMBODIED_IMPACT_GWP = 143
GPU_EMBODIED_IMPACT_ADPE = 5.1e-3
GPU_EMBODIED_IMPACT_PE = 1828

SERVER_GPUS = 8
SERVER_POWER = 1  # kW
SERVER_EMBODIED_IMPACT_GWP = 3000
SERVER_EMBODIED_IMPACT_ADPE = 0.24
SERVER_EMBODIED_IMPACT_PE = 38000

HARDWARE_LIFESPAN = 5 * 365 * 24 * 60 * 60

PROVIDER_WUE_ONSITE = { #Water use efficiency on-site, as opposed to off-site generated energy
    "Google" : 0.916,
    "Meta": 0.18,    # L/kWh, 2023
    "Microsoft": 0.49, #2022
    "OVHCloud": 0.37, #2024
    "Scaleway": 0.216, #2023
    "AWS" : 0.18, #2023
    "Equinix" : 1.07 #2023
}


PROVIDER_PUE = { #Power use efficiency
    "Google" : 1.09,
    "Meta" : 1.09,
    "Microsoft" : 1.18,
    "OVHCloud" : 1.26,
    "Scaleway" : 1.37,
    "AWS" : 1.15,
    "Equinix" : 1.42
}

#A list that draws the connection between AI companies and their data center providers
AI_COMPANY_TO_DATA_CENTER_PROVIDER = {
    "anthropic"	: "Google",
    "mistralai"	: "OVHCloud",
    "cohere"	: "AWS",
    "databricks" : "Microsoft",
    "meta"	: "Meta",
    "azureopenai" : "Microsoft", #treated the same way as OpenAI
    "huggingface_hub" : "AWS",
    "google" : "Google",
    "microsoft"	: "Microsoft",
    "openai" : "Microsoft",
    "litellm" : "AWS" #need a way to identify provider from model inputed
}

BATCHING_SIZE = 16

GPUS_IN_SERVER = 8

WATER_FABRICATING_GPU = 0.56178343949
# https://waferpro.com/how-many-chips-can-be-cut-from-a-silicon-wafer/?srsltid=AfmBOoriSA25IQoHzZsc2-7QC8kMqAn8GRsnDFlA0OcSnvNFPFH0zUH8
# Estimate of Chips per 300mm Wafer
# Assume a 15mm x 15mm chip size:

# 300mm wafer: ~70,685 mm² area (π * (150mm)²)
# 15mm x 15mm chip: 225 mm²
# So, the simple calculation would be:

# 70,685 mm2 / 225 mm2 ​≈ 314 chips

# https://esg.tsmc.com/en-US/file/public/e-all_2023.pdf
# page 114, 2023
# 2023 - 176.4 Water consumption per wafer-layer (Liter/12-inch equivalent wafer mask layer)
# 176.4/314 =
# 0.56178343949 L/chip


dag = DAG()


@dag.asset
def gpu_energy(
        model_active_parameter_count: float,
        output_token_count: float,
        gpu_energy_alpha: float,
        gpu_energy_beta: float,
        gpu_energy_stdev: float
) -> ValueOrRange:
    """
    Compute energy consumption of a single GPU.

    Args:
        model_active_parameter_count: Number of active parameters of the model (in billion).
        output_token_count: Number of generated tokens.
        gpu_energy_alpha: Alpha parameter of the GPU linear power consumption profile.
        gpu_energy_beta: Beta parameter of the GPU linear power consumption profile.
        gpu_energy_stdev: Standard deviation of the GPU linear power consumption profile.

    Returns:
        The 95% confidence interval of energy consumption of a single GPU in kWh.
    """
    gpu_energy_per_token_mean = gpu_energy_alpha * model_active_parameter_count + gpu_energy_beta
    gpu_energy_min = output_token_count * (gpu_energy_per_token_mean - 1.96 * gpu_energy_stdev)
    gpu_energy_max = output_token_count * (gpu_energy_per_token_mean + 1.96 * gpu_energy_stdev)
    return RangeValue(min=max(0, gpu_energy_min), max=gpu_energy_max)

@dag.asset
def generation_latency(
        model_active_parameter_count: float,
        output_token_count: float,
        gpu_latency_alpha: float,
        gpu_latency_beta: float,
        gpu_latency_stdev: float,
        request_latency: float,
) -> ValueOrRange:
    """
    Compute the token generation latency in seconds.

    Args:
        model_active_parameter_count: Number of active parameters of the model (in billion).
        output_token_count: Number of generated tokens.
        gpu_latency_alpha: Alpha parameter of the GPU linear latency profile.
        gpu_latency_beta: Beta parameter of the GPU linear latency profile.
        gpu_latency_stdev: Standard deviation of the GPU linear latency profile.
        request_latency: Measured request latency (upper bound) in seconds.

    Returns:
        The token generation latency in seconds.
    """
    gpu_latency_per_token_mean = gpu_latency_alpha * model_active_parameter_count + gpu_latency_beta
    gpu_latency_min = output_token_count * (gpu_latency_per_token_mean - 1.96 * gpu_latency_stdev)
    gpu_latency_max = output_token_count * (gpu_latency_per_token_mean + 1.96 * gpu_latency_stdev)
    gpu_latency_interval = RangeValue(min=max(0, gpu_latency_min), max=gpu_latency_max)
    if gpu_latency_interval < request_latency:
        return gpu_latency_interval
    return request_latency

@dag.asset
def model_required_memory(
        model_total_parameter_count: float,
        model_quantization_bits: int,
) -> float:
    """
    Compute the required memory to load the model on GPU.

    Args:
        model_total_parameter_count: Number of parameters of the model (in billion).
        model_quantization_bits: Number of bits used to represent the model weights.

    Returns:
        The amount of required GPU memory to load the model.
    """
    return 1.2 * model_total_parameter_count * model_quantization_bits / 8


@dag.asset
def gpu_required_count(
        model_required_memory: float,
        gpu_memory: float
) -> int:
    """
    Compute the number of required GPU to store the model.

    Args:
        model_required_memory: Required memory to load the model on GPU.
        gpu_memory: Amount of memory available on a single GPU.

    Returns:
        The number of required GPUs to load the model.
    """
    return ceil(model_required_memory / gpu_memory)


@dag.asset
def server_energy(
        generation_latency: float,
        server_power: float,
        server_gpu_count: int,
        gpu_required_count: int
) -> float:
    """
    Compute the energy consumption of the server.

    Args:
        generation_latency: Token generation latency in seconds.
        server_power: Power consumption of the server in kW.
        server_gpu_count: Number of available GPUs in the server.
        gpu_required_count: Number of required GPUs to load the model.

    Returns:
        The energy consumption of the server (GPUs are not included) in kWh.
    """
    return (generation_latency / 3600) * server_power * (gpu_required_count / server_gpu_count)


@dag.asset
def request_energy(
        provider: str,
        provider_pue: dict,
        ai_company_to_data_center_provider: dict,
        server_energy: float,
        gpu_required_count: int,
        gpu_energy: ValueOrRange
) -> ValueOrRange:
    """
    Compute the energy consumption of the request.

    Args:
        provider: The provider of AI that we are measuring
        provider_pue: Power usage efficiency. Depends on the data center provider.
        ai_company_to_data_center_provider: A dictionary mapping AI providers to their data center providers.
        server_energy: Energy consumption of the server in kWh.
        gpu_required_count: Number of required GPUs to load the model.
        gpu_energy: Energy consumption of a single GPU in kWh.

    Returns:
        The energy consumption of the request in kWh.
    """
    results = (provider_pue[ai_company_to_data_center_provider[provider]] *
               (server_energy + gpu_required_count * gpu_energy))
    return results


@dag.asset
def request_usage_gwp(
        request_energy: ValueOrRange,
        if_electricity_mix_gwp: float
) -> ValueOrRange:
    """
    Compute the Global Warming Potential (GWP) usage impact of the request.

    Args:
        request_energy: Energy consumption of the request in kWh.
        if_electricity_mix_gwp: GWP impact factor of electricity consumption in kgCO2eq / kWh.

    Returns:
        The GWP usage impact of the request in kgCO2eq.
    """
    return request_energy * if_electricity_mix_gwp


@dag.asset
def request_usage_adpe(
        request_energy: ValueOrRange,
        if_electricity_mix_adpe: float
) -> ValueOrRange:
    """
    Compute the Abiotic Depletion Potential for Elements (ADPe) usage impact of the request.

    Args:
        request_energy: Energy consumption of the request in kWh.
        if_electricity_mix_adpe: ADPe impact factor of electricity consumption in kgSbeq / kWh.

    Returns:
        The ADPe usage impact of the request in kgSbeq.
    """
    return request_energy * if_electricity_mix_adpe


@dag.asset
def request_usage_pe(
        request_energy: ValueOrRange,
        if_electricity_mix_pe: float
) -> ValueOrRange:
    """
    Compute the Primary Energy (PE) usage impact of the request.

    Args:
        request_energy: Energy consumption of the request in kWh.
        if_electricity_mix_pe: PE impact factor of electricity consumption in MJ / kWh.

    Returns:
        The PE usage impact of the request in MJ.
    """
    return request_energy * if_electricity_mix_pe

@dag.asset
def request_usage_water(
        request_energy: ValueOrRange,
        if_electricity_mix_wcf: float,
        provider_wue_onsite: dict,
        provider: str,
        provider_pue: dict,
        ai_company_to_data_center_provider: dict
) -> ValueOrRange:
    """
    Compute the water usage impact of the request.

    Args:
        request_energy: Energy consumption of the request in kWh.
        if_electricity_mix_wcf: Water consumption factor off-site, water consumption to electricity cosnumption.
            Depends on the data center's location.
        provider_wue_onsite: Water consumption factor on-site. Depends on the data center.
        provider: The provider of AI that we are measuring
        provider_pue: Power usage efficiency. Depends on the data center provider.
        ai_company_to_data_center_provider: A dictionary mapping AI providers to their data center providers.
    Returns:
        The water usage impact of the request in liters.
    """



    output = request_energy * (provider_wue_onsite[ai_company_to_data_center_provider[provider]] +
    provider_pue[ai_company_to_data_center_provider[provider]] * if_electricity_mix_wcf )

    return output


@dag.asset
def server_gpu_embodied_gwp(
        server_embodied_gwp: float,
        server_gpu_count: float,
        gpu_embodied_gwp: float,
        gpu_required_count: int
) -> float:
    """
    Compute the Global Warming Potential (GWP) embodied impact of the server

    Args:
        server_embodied_gwp: GWP embodied impact of the server in kgCO2eq.
        server_gpu_count: Number of available GPUs in the server.
        gpu_embodied_gwp: GWP embodied impact of a single GPU in kgCO2eq.
        gpu_required_count: Number of required GPUs to load the model.

    Returns:
        The GWP embodied impact of the server and the GPUs in kgCO2eq.
    """
    return (gpu_required_count / server_gpu_count) * server_embodied_gwp + gpu_required_count * gpu_embodied_gwp


@dag.asset
def server_gpu_embodied_adpe(
        server_embodied_adpe: float,
        server_gpu_count: float,
        gpu_embodied_adpe: float,
        gpu_required_count: int
) -> float:
    """
    Compute the Abiotic Depletion Potential for Elements (ADPe) embodied impact of the server

    Args:
        server_embodied_adpe: ADPe embodied impact of the server in kgSbeq.
        server_gpu_count: Number of available GPUs in the server.
        gpu_embodied_adpe: ADPe embodied impact of a single GPU in kgSbeq.
        gpu_required_count: Number of required GPUs to load the model.

    Returns:
        The ADPe embodied impact of the server and the GPUs in kgSbeq.
    """
    return (gpu_required_count / server_gpu_count) * server_embodied_adpe + gpu_required_count * gpu_embodied_adpe


@dag.asset
def server_gpu_embodied_pe(
        server_embodied_pe: float,
        server_gpu_count: float,
        gpu_embodied_pe: float,
        gpu_required_count: int
) -> float:
    """
    Compute the Primary Energy (PE) embodied impact of the server

    Args:
        server_embodied_pe: PE embodied impact of the server in MJ.
        server_gpu_count: Number of available GPUs in the server.
        gpu_embodied_pe: PE embodied impact of a single GPU in MJ.
        gpu_required_count: Number of required GPUs to load the model.

    Returns:
        The PE embodied impact of the server and the GPUs in MJ.
    """
    return (gpu_required_count / server_gpu_count) * server_embodied_pe + gpu_required_count * gpu_embodied_pe


@dag.asset
def request_embodied_gwp(
        server_gpu_embodied_gwp: float,
        server_lifetime: float,
        generation_latency: ValueOrRange
) -> ValueOrRange:
    """
    Compute the Global Warming Potential (GWP) embodied impact of the request.

    Args:
        server_gpu_embodied_gwp: GWP embodied impact of the server and the GPUs in kgCO2eq.
        server_lifetime: Lifetime duration of the server in seconds.
        generation_latency: Token generation latency in seconds.

    Returns:
        The GWP embodied impact of the request in kgCO2eq.
    """
    return (generation_latency / server_lifetime) * server_gpu_embodied_gwp


@dag.asset
def request_embodied_adpe(
        server_gpu_embodied_adpe: float,
        server_lifetime: float,
        generation_latency: ValueOrRange
) -> ValueOrRange:
    """
    Compute the Abiotic Depletion Potential for Elements (ADPe) embodied impact of the request.

    Args:
        server_gpu_embodied_adpe: ADPe embodied impact of the server and the GPUs in kgSbeq.
        server_lifetime: Lifetime duration of the server in seconds.
        generation_latency: Token generation latency in seconds.

    Returns:
        The ADPe embodied impact of the request in kgSbeq.
    """
    return (generation_latency / server_lifetime) * server_gpu_embodied_adpe


@dag.asset
def request_embodied_pe(
        server_gpu_embodied_pe: float,
        server_lifetime: float,
        generation_latency: ValueOrRange
) -> ValueOrRange:
    """
    Compute the Primary Energy (PE) embodied impact of the request.

    Args:
        server_gpu_embodied_pe: PE embodied impact of the server and the GPUs in MJ.
        server_lifetime: Lifetime duration of the server in seconds.
        generation_latency: Token generation latency in seconds.

    Returns:
        The PE embodied impact of the request in MJ.
    """
    return (generation_latency / server_lifetime) * server_gpu_embodied_pe


@dag.asset
def request_embodied_water(
        server_lifetime: float,
        batching_size: float,
        water_fabricating_gpu: float,
        gpus_in_server: float,
        generation_latency: ValueOrRange
) -> ValueOrRange:
    """
    Compute the water embodied impact of the request.

    Args:
        server_lifetime: Lifetime duration of the server in seconds.
        generation_latency: Token generation latency in seconds.
        water_fabricating_gpu: The amount of water used in fabricating a gpu.
        gpus_in_server: The number of GPUs in a server.
        batching_size: The number of requests handled concurrently by the server.

    Returns:
        The water embodied impact of the request in liters.
    """

    output = generation_latency *water_fabricating_gpu * gpus_in_server/ (server_lifetime * batching_size)

    return output



def compute_llm_impacts_dag(
        provider: str,
        model_active_parameter_count: ValueOrRange,
        model_total_parameter_count: ValueOrRange,
        output_token_count: float,
        request_latency: float,
        if_electricity_mix_adpe: float,
        if_electricity_mix_pe: float,
        if_electricity_mix_gwp: float,
        if_electricity_mix_wcf: float,
        model_quantization_bits: Optional[int] = MODEL_QUANTIZATION_BITS,
        gpu_energy_alpha: Optional[float] = GPU_ENERGY_ALPHA,
        gpu_energy_beta: Optional[float] = GPU_ENERGY_BETA,
        gpu_energy_stdev: Optional[float] = GPU_ENERGY_STDEV,
        gpu_latency_alpha: Optional[float] = GPU_LATENCY_ALPHA,
        gpu_latency_beta: Optional[float] = GPU_LATENCY_BETA,
        gpu_latency_stdev: Optional[float] = GPU_LATENCY_STDEV,
        gpu_memory: Optional[float] = GPU_MEMORY,
        gpu_embodied_gwp: Optional[float] = GPU_EMBODIED_IMPACT_GWP,
        gpu_embodied_adpe: Optional[float] = GPU_EMBODIED_IMPACT_ADPE,
        gpu_embodied_pe: Optional[float] = GPU_EMBODIED_IMPACT_PE,
        server_gpu_count: Optional[int] = SERVER_GPUS,
        server_power: Optional[float] = SERVER_POWER,
        server_embodied_gwp: Optional[float] = SERVER_EMBODIED_IMPACT_GWP,
        server_embodied_adpe: Optional[float] = SERVER_EMBODIED_IMPACT_ADPE,
        server_embodied_pe: Optional[float] = SERVER_EMBODIED_IMPACT_PE,
        server_lifetime: Optional[float] = HARDWARE_LIFESPAN,
        provider_wue_onsite: Optional[dict] = PROVIDER_WUE_ONSITE,
        provider_pue: Optional[dict] = PROVIDER_PUE,
        ai_company_to_data_center_provider: Optional[dict] = AI_COMPANY_TO_DATA_CENTER_PROVIDER,
        water_fabricating_gpu: Optional[float] = WATER_FABRICATING_GPU,
        gpus_in_server: Optional[float] = GPUS_IN_SERVER,
        batching_size: Optional[float] =  BATCHING_SIZE
) -> dict[str, ValueOrRange]:
    """
    Compute the impacts dag of an LLM generation request.

    Args:
        provider: The provider of the model
        model_active_parameter_count: Number of active parameters of the model (in billion).
        model_total_parameter_count: Number of parameters of the model (in billion).
        output_token_count: Number of generated tokens.
        request_latency: Measured request latency in seconds.
        if_electricity_mix_adpe: ADPe impact factor of electricity consumption of kgSbeq / kWh (Antimony).
        if_electricity_mix_pe: PE impact factor of electricity consumption in MJ / kWh.
        if_electricity_mix_gwp: GWP impact factor of electricity consumption in kgCO2eq / kWh.
        if_electricity_mix_wcf: Water consumption factor, water consumption to electricity consumption in liters / kWh.
        model_quantization_bits: Number of bits used to represent the model weights.
        gpu_energy_alpha: Alpha parameter of the GPU linear power consumption profile.
        gpu_energy_beta: Beta parameter of the GPU linear power consumption profile.
        gpu_energy_stdev: Standard deviation of the GPU linear power consumption profile.
        gpu_latency_alpha: Alpha parameter of the GPU linear latency profile.
        gpu_latency_beta: Beta parameter of the GPU linear latency profile.
        gpu_latency_stdev: Standard deviation of the GPU linear latency profile.
        gpu_memory: Amount of memory available on a single GPU.
        gpu_embodied_gwp: GWP embodied impact of a single GPU.
        gpu_embodied_adpe: ADPe embodied impact of a single GPU.
        gpu_embodied_pe: PE embodied impact of a single GPU.
        server_gpu_count: Number of available GPUs in the server.
        server_power: Power consumption of the server in kW.
        server_embodied_gwp: GWP embodied impact of the server in kgCO2eq.
        server_embodied_adpe: ADPe embodied impact of the server in kgSbeq.
        server_embodied_pe: PE embodied impact of the server in MJ.
        server_lifetime: Lifetime duration of the server in seconds.
        provider_wue_onsite: Water consumption factor on-site. Depends on the data center.
        provider_pue: Power usage efficiency. Depends on the data center provider.
        ai_company_to_data_center_provider: A dictionary mapping AI providers to their data center providers.
        water_fabricating_gpu: The amount of water used in fabricating a gpu.
        gpus_in_server: The number of GPUs in a server, default set to 8.
        batching_size: The number of requests handled concurrently by the server, default set to 16.
    Returns:
        The impacts dag with all intermediate states.
    """
    results = dag.execute(
        provider=provider,
        model_active_parameter_count=model_active_parameter_count,
        model_total_parameter_count=model_total_parameter_count,
        model_quantization_bits=model_quantization_bits,
        output_token_count=output_token_count,
        request_latency=request_latency,
        if_electricity_mix_gwp=if_electricity_mix_gwp,
        if_electricity_mix_adpe=if_electricity_mix_adpe,
        if_electricity_mix_pe=if_electricity_mix_pe,
        if_electricity_mix_wcf=if_electricity_mix_wcf,
        gpu_energy_alpha=gpu_energy_alpha,
        gpu_energy_beta=gpu_energy_beta,
        gpu_energy_stdev=gpu_energy_stdev,
        gpu_latency_alpha=gpu_latency_alpha,
        gpu_latency_beta=gpu_latency_beta,
        gpu_latency_stdev=gpu_latency_stdev,
        gpu_memory=gpu_memory,
        gpu_embodied_gwp=gpu_embodied_gwp,
        gpu_embodied_adpe=gpu_embodied_adpe,
        gpu_embodied_pe=gpu_embodied_pe,
        server_gpu_count=server_gpu_count,
        server_power=server_power,
        server_embodied_gwp=server_embodied_gwp,
        server_embodied_adpe=server_embodied_adpe,
        server_embodied_pe=server_embodied_pe,
        server_lifetime=server_lifetime,
        provider_wue_onsite=provider_wue_onsite,
        provider_pue=provider_pue,
        ai_company_to_data_center_provider=ai_company_to_data_center_provider,
        water_fabricating_gpu=water_fabricating_gpu,
        gpus_in_server=gpus_in_server,
        batching_size=batching_size
    )
    return results

def compute_llm_impacts(
        model_active_parameter_count: ValueOrRange,
        model_total_parameter_count: ValueOrRange,
        output_token_count: float,
        if_electricity_mix_adpe: float,
        if_electricity_mix_pe: float,
        if_electricity_mix_gwp: float,
        if_electricity_mix_wcf:float,
        request_latency: Optional[float] = None,
        **kwargs: Any
) -> Impacts:
    """
    Compute the impacts of an LLM generation request.

    Args:
        provider: The provider of the model
        model_active_parameter_count: Number of active parameters of the model (in billion).
        model_total_parameter_count: Number of total parameters of the model (in billion).
        output_token_count: Number of generated tokens.
        if_electricity_mix_adpe: ADPe impact factor of electricity consumption of kgSbeq / kWh (Antimony).
        if_electricity_mix_pe: PE impact factor of electricity consumption in MJ / kWh.
        if_electricity_mix_gwp: GWP impact factor of electricity consumption in kgCO2eq / kWh.
        if_electricity_mix_wcf: Water consumption factor, water consumption to electricity consumption in liters / kWh.
        request_latency: Measured request latency in seconds.
        **kwargs: Any other optional parameter.

    Returns:
        The impacts of an LLM generation request.
    """
    if request_latency is None:
        request_latency = math.inf

    active_params = [model_active_parameter_count]
    total_params = [model_total_parameter_count]

    if isinstance(model_active_parameter_count, RangeValue) or isinstance(model_total_parameter_count, RangeValue):
        if isinstance(model_active_parameter_count, RangeValue):
            active_params = [model_active_parameter_count.min, model_active_parameter_count.max]
        else:
            active_params = [model_active_parameter_count, model_active_parameter_count]
        if isinstance(model_total_parameter_count, RangeValue):
            total_params = [model_total_parameter_count.min, model_total_parameter_count.max]
        else:
            total_params = [model_total_parameter_count, model_total_parameter_count]

    results: dict[str, Union[RangeValue, float, int]] = {}
    fields = ["request_energy", "request_usage_gwp", "request_usage_adpe", "request_usage_pe", "request_usage_water",
              "request_embodied_gwp", "request_embodied_adpe", "request_embodied_pe", "request_embodied_water"]
    for act_param, tot_param in zip(active_params, total_params):
        res = compute_llm_impacts_dag(
            provider=EcoLogits.config.provider_selected,
            model_active_parameter_count=act_param,
            model_total_parameter_count=tot_param,
            output_token_count=output_token_count,
            request_latency=request_latency,
            if_electricity_mix_adpe=if_electricity_mix_adpe,
            if_electricity_mix_pe=if_electricity_mix_pe,
            if_electricity_mix_gwp=if_electricity_mix_gwp,
            if_electricity_mix_wcf=if_electricity_mix_wcf,
            **kwargs
        )
        for field in fields:
            if field in results:
                min_result = results[field]
                max_result = res[field]
                if isinstance(min_result, RangeValue):
                    min_result = cast(Union[float, int], min_result.min)
                if isinstance(max_result, RangeValue):
                    max_result = cast(Union[float, int], max_result.max)
                results[field] = RangeValue(min=min_result, max=max_result)
            else:
                results[field] = res[field]

    energy = Energy(value=results["request_energy"])
    gwp_usage = GWP(value=results["request_usage_gwp"])
    adpe_usage = ADPe(value=results["request_usage_adpe"])
    pe_usage = PE(value=results["request_usage_pe"])
    water_usage = Water(value=results["request_usage_water"])
    gwp_embodied = GWP(value=results["request_embodied_gwp"])
    adpe_embodied = ADPe(value=results["request_embodied_adpe"])
    pe_embodied = PE(value=results["request_embodied_pe"])
    water_embodied = Water(value=results["request_embodied_water"])

    return Impacts(
        energy=energy,
        gwp=gwp_usage + gwp_embodied,
        adpe=adpe_usage + adpe_embodied,
        pe=pe_usage + pe_embodied,
        water=water_usage + water_embodied,
        usage=Usage(
            energy=energy,
            gwp=gwp_usage,
            adpe=adpe_usage,
            pe=pe_usage,
            water = water_usage
        ),
        embodied=Embodied(
            gwp=gwp_embodied,
            adpe=adpe_embodied,
            pe=pe_embodied,
            water=water_embodied
        )
    )
