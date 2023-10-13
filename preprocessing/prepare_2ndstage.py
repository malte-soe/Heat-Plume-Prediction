import argparse
import logging
import os
import pathlib
import torch
import time
import yaml

from torch import stack, load
from tqdm.auto import tqdm

from networks.unet import UNet
from preprocessing.prepare_1ststage import prepare_dataset
from domain_classes.domain import Domain
from domain_classes.heat_pump import HeatPump
from domain_classes.utils_2hp import save_config_of_separate_inputs, save_config_of_merged_inputs, save_yaml
from domain_classes.stitching import Stitching
from utils.prepare_paths import Paths2HP


def prepare_dataset_for_2nd_stage(paths: Paths2HP, dataset_name: str, inputs_1hp: str, device: str = "cuda:0"):
    """
    assumptions:
    - 1hp-boxes are generated already
    - 1hpnn is trained
    - cell sizes of 1hp-boxes and domain are the same
    - boundaries of boxes around at least one hp is within domain
    - device: attention, all stored need to be produced on cpu for later pin_memory=True and all other can be gpu
    """
    
    timestamp_begin = time.ctime()
    time_begin = time.perf_counter()

# prepare domain dataset if not yet done
    ## load model from 1st stage
    time_start_prep_domain = time.perf_counter()
    model_1HP = UNet(in_channels=len(inputs_1hp)).float()
    model_1HP.load_state_dict(load(f"{paths.model_1hp_path}/model.pt", map_location=device))
    # model_1HP.to(device)
    
    ## prepare 2hp dataset for 1st stage
    if not os.path.exists(paths.dataset_1st_prep_path):        
        # norm with data from dataset that NN was trained with!!
        with open(os.path.join(os.getcwd(), paths.dataset_model_trained_with_prep_path, "info.yaml"), "r") as file:
            info = yaml.safe_load(file)
        prepare_dataset(paths, dataset_name, inputs_1hp, info=info, power2trafo=False)
    print(f"Domain prepared ({paths.dataset_1st_prep_path})")

# prepare dataset for 2nd stage
    time_start_prep_2hp = time.perf_counter()
    avg_time_inference_1hp = 0
    list_runs = os.listdir(os.path.join(paths.dataset_1st_prep_path, "Inputs"))
    for run_file in tqdm(list_runs, desc="2HP prepare", total=len(list_runs)):
        run_id = f'{run_file.split(".")[0]}_'
        domain = Domain(paths.dataset_1st_prep_path, stitching_method="max", file_name=run_file)
        ## generate 1hp-boxes and extract information like perm and ids etc.
        if domain.skip_datapoint:
            logging.warning(f"Skipping {run_id}")
            continue

        single_hps = domain.extract_hp_boxes()
        # apply learned NN to predict the heat plumes
        hp: HeatPump
        for hp in single_hps:
            time_start_run_1hp = time.perf_counter()
            hp.primary_temp_field = hp.apply_nn(model_1HP)
            avg_time_inference_1hp += time.perf_counter() - time_start_run_1hp
            hp.primary_temp_field = domain.reverse_norm(hp.primary_temp_field, property="Temperature [C]")
        avg_time_inference_1hp /= len(single_hps)

        for hp in single_hps:
            hp.get_other_temp_field(single_hps)

        for hp in single_hps:
            hp.primary_temp_field = domain.norm(hp.primary_temp_field, property="Temperature [C]")
            hp.other_temp_field = domain.norm(hp.other_temp_field, property="Temperature [C]")
            inputs = stack([hp.primary_temp_field, hp.other_temp_field])
            hp.save(run_id=run_id, dir=paths.datasets_boxes_prep_path, inputs_all=inputs,)

    time_end = time.perf_counter()
    avg_inference_times = avg_time_inference_1hp / len(list_runs)

    # save infos of info file about separated (only 2!) inputs
    save_config_of_separate_inputs(domain.info, path=paths.datasets_boxes_prep_path)

    # save measurements
    with open(os.path.join(os.getcwd(), "runs", paths.datasets_boxes_prep_path, f"measurements.yaml"), "w") as f:
        f.write(f"timestamp of beginning: {timestamp_begin}\n")
        f.write(f"timestamp of end: {time.ctime()}\n")
        f.write(f"model 1HP: {paths.model_1hp_path}\n")
        f.write(f"input params: {inputs_1hp}\n")
        f.write(f"separate inputs: {True}\n")
        f.write(f"location of prepared domain dataset: {paths.dataset_1st_prep_path}\n")
        f.write(f"name of dataset prepared with: {paths.dataset_model_trained_with_prep_path}\n")
        f.write(f"name of dataset domain: {dataset_name}\n")
        f.write(f"name_destination_folder: {paths.datasets_boxes_prep_path}\n")
        f.write(f"avg inference times for 1HP-NN in seconds: {avg_inference_times}\n")
        f.write(f"device: {device}\n")
        f.write(f"duration of preparing domain in seconds: {(time_start_prep_2hp-time_start_prep_domain)}\n")
        f.write(f"duration of preparing 2HP in seconds: {(time_end-time_start_prep_2hp)}\n")
        f.write(f"duration of preparing 2HP /run in seconds: {(time_end-time_start_prep_2hp)/len(list_runs)}\n")
        f.write(f"duration of whole process in seconds: {(time_end-time_begin)}\n")


def merge_inputs_for_2HPNN(path_separate_inputs:pathlib.Path, path_merged_inputs:pathlib.Path, stitching_method:str="max"):
    begin = time.perf_counter()
    assert stitching_method == "max", "Other than max stitching required reasonable background temp and therefor potentially norming."
    stitching = Stitching(stitching_method, background_temperature=0)
    
    (path_merged_inputs/"Inputs").mkdir(exist_ok=True)

    begin_prep = time.perf_counter()
    # get separate inputs if exist
    for file in (path_separate_inputs/"Inputs").iterdir():
        input = torch.load(file)
        # merge inputs via stitching
        input = stitching(input[0], input[1])
        # save merged inputs
        input = torch.unsqueeze(torch.Tensor(input), 0)
        torch.save(input, path_merged_inputs/"Inputs"/file.name)
    end_prep = time.perf_counter()

    # save config of merged inputs
    info_separate = yaml.load(open(path_separate_inputs/"info.yaml", "r"), Loader=yaml.FullLoader)
    save_config_of_merged_inputs(info_separate, path_merged_inputs)

    # save command line arguments
    cla = {
        "dataset_separate": path_separate_inputs.name,
        "command": "prepare_2HP_merged_inputs.py"
    }
    save_yaml(cla, path=path_merged_inputs, name_file="command_line_args")
    end = time.perf_counter()

    # save times in measurements.yaml (also copy relevant ones from separate)
    measurements_prep_separate = yaml.load(open(path_separate_inputs/"measurements.yaml", "r"), Loader=yaml.FullLoader)
    num_dp = len(list((path_separate_inputs/"Inputs").iterdir()))
    duration_prep = end_prep - begin_prep
    duration_prep_avg = duration_prep / num_dp
    measurements = {
        "duration of preparation in seconds": duration_prep,
        "duration of preparing 2HP /run in seconds": duration_prep_avg,
        "duration total in seconds": end - begin,
        "number of datapoints": num_dp,
        "separate-preparation": {"duration of preparing domain in seconds": measurements_prep_separate["duration of preparing domain in seconds"],
                                    "duration of preparing 2HP /run in seconds": measurements_prep_separate["duration of preparing 2HP /run in seconds"],
                                    "duration of preparing 2HP in seconds": measurements_prep_separate["duration of preparing 2HP in seconds"],
                                    "duration of whole process in seconds": measurements_prep_separate["duration of whole process in seconds"]},
    }
    save_yaml(measurements, path=path_merged_inputs, name_file="measurements")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=str, default="dataset_2hps_1fixed_10dp_2hp_gksi_1000dp")
    parser.add_argument("--merge_inputs", type=bool, default=False)
    args = parser.parse_args()

    #get dir of prepare_2HP_separate_inputs
    paths = yaml.load(open("paths.yaml", "r"), Loader=yaml.FullLoader)
    dir_separate_inputs = paths["datasets_prepared_2hp_dir"]
    path_separate_inputs = pathlib.Path(dir_separate_inputs) / args.dataset

    if args.merge_inputs:

        path_merged_inputs = pathlib.Path(dir_separate_inputs) / (args.dataset+"_merged")
        path_merged_inputs.mkdir(exist_ok=True)
        # copy "Labels" folder from separate to merged
        os.system(f"cp -r {path_separate_inputs/'Labels'} {path_merged_inputs}")

        if os.path.exists(path_separate_inputs):
            merge_inputs_for_2HPNN(path_separate_inputs, path_merged_inputs, stitching_method="max")
        else:
            print(f"Could not find prepared dataset with separate inputs at {path_separate_inputs}.")
    else:
        if os.path.exists(path_separate_inputs):
            print("You need to set --merge_inputs=True to merge inputs otherwise you're done. Your separate inputs are already prepared.")
        else:
            print(f"Could not find prepared dataset with separate inputs at {path_separate_inputs}. Please go to file main.py for that.")