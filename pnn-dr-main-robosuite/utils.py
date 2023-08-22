# -*- coding: utf-8 -*-
import os
import plotly
from plotly.graph_objs import Scatter, Line
import torch
from torch import multiprocessing as mp

# Global counter
class Counter:
    def __init__(self):
        """
        Class constructor.
        """
        self.val = mp.Value("i", 0)
        self.lock = mp.Lock()

    def increment(self):
        """
        Increments in one unit the counter value.
        """
        with self.lock:
            self.val.value += 1

    def value(self):
        """
        Obtain the counter value.

        Returns:
            int: counter value.
        """
        with self.lock:
            return self.val.value


def state_to_tensor(state):
    """
    Converts a state from the OpenAI Gym (a numpy array) to a batch tensor.

    Args:
        state (tuple): joints' variables and image observation.

    Returns:
        tuple: non-rgb state and rgb state tensors.
    """
    # Copies numpy arrays as they have negative strides
    # return (
    #     torch.Tensor(state[0]).unsqueeze(0),
    #     (torch.from_numpy(state[1].copy())).permute(2, 1, 0).float().div_(255).unsqueeze(0),
    # )

    return (
        torch.from_numpy(state.copy()).permute(2, 1, 0).float().div_(255).unsqueeze(0)
    )


def plot_line(xs, ys_population):
    """
    Plots min, max and mean + standard deviation bars of a population over time.

    Args:
        xs (list): values for the x-axis.
        ys_population (list): values for the y-axis.
    """
    max_colour = "rgb(0, 132, 180)"
    mean_colour = "rgb(0, 172, 237)"
    std_colour = "rgba(29, 202, 255, 0.2)"

    ys = torch.Tensor(ys_population)
    ys_min = ys.min(1)[0].squeeze()
    ys_max = ys.max(1)[0].squeeze()
    ys_mean = ys.mean(1).squeeze()
    ys_std = ys.std(1).squeeze()
    ys_upper, ys_lower = ys_mean + ys_std, ys_mean - ys_std

    trace_max = Scatter(x=xs, y=ys_max.numpy(), line=Line(color=max_colour, dash="dash"), name="Max")
    trace_upper = Scatter(
        x=xs,
        y=ys_upper.numpy(),
        line=Line(color="transparent"),
        name="+1 Std. Dev.",
        showlegend=False,  # transparent
    )
    trace_mean = Scatter(
        x=xs,
        y=ys_mean.numpy(),
        fill="tonexty",
        fillcolor=std_colour,
        line=Line(color=mean_colour),
        name="Mean",
    )
    trace_lower = Scatter(
        x=xs,
        y=ys_lower.numpy(),
        fill="tonexty",
        fillcolor=std_colour,
        line=Line(color="transparent"),  # transparent
        name="-1 Std. Dev.",
        showlegend=False,
    )
    trace_min = Scatter(x=xs, y=ys_min.numpy(), line=Line(color=max_colour, dash="dash"), name="Min")

    plotly.offline.plot(
        {
            "data": [trace_upper, trace_mean, trace_lower, trace_min, trace_max],
            "layout": dict(
                title="Rewards",
                xaxis={"title": "Step"},
                yaxis={"title": "Average Reward"},
            ),
        },
        filename=os.path.join("results", "rewards.html"),
        auto_open=False,
    )
