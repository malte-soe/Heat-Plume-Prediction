import torch
import os
import numpy as np
import yaml
from torch.utils.data import Dataset
import pathlib
from data.transforms import NormalizeTransform

class SimulationDataset(Dataset):
    def __init__(self, path):
        Dataset.__init__(self)
        self.path = pathlib.Path(path)
        self.input_names = []
        self.label_names = []
        for filename in os.listdir(self.path.joinpath("Inputs")):
            self.input_names.append(filename)
        for filename in os.listdir(self.path.joinpath("Labels")):
            self.label_names.append(filename)
        self.input_names.sort()
        self.label_names.sort()
        self.info = self.__load_info()
        self.norm = NormalizeTransform(self.info)

        if len(self.input_names) != len(self.label_names):
            raise ValueError(
                "Number of Inputs and labels does not match!")

    @property
    def input_channels(self):
        return len(self.info["Inputs"])

    @property
    def output_channels(self):
        return len(self.info["Labels"])

    def __load_info(self):
        with open(self.path.joinpath("info.yaml"), "r") as f:
            info = yaml.safe_load(f)
        return info

    def __len__(self):
        return len(self.input_names)

    def __getitem__(self, index):
        input = torch.load(self.path.joinpath(
            "Inputs", self.input_names[index]))
        label = torch.load(self.path.joinpath(
            "Labels", self.label_names[index]))
        return input, label
    
    def get_run_id(self, index):
        return self.input_names[index]
