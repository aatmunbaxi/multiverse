
### ~~~
## ~~~ Import block
### ~~~

#
# ~~~ Standard packages
import torch
from torch import nn, optim
from tqdm import tqdm
from statistics import mean as avg
from matplotlib import pyplot as plt
from importlib import import_module
from time import time
import argparse
import sys
import os

#
# ~~~ Package-specific utils
from bnns.utils import plot_nn, plot_bnn_mean_and_std, plot_bnn_empirical_quantiles, generate_json_filename, set_Dataset_attributes, EarlyStopper
from bnns.metrics import *

#
# ~~~ My Personal Helper Functions (https://github.com/ThomasLastName/quality_of_life)
from quality_of_life.my_visualization_utils import GifMaker
from quality_of_life.my_base_utils          import support_for_progress_bars, dict_to_json, json_to_dict, print_dict, my_warn, process_for_saving
from quality_of_life.my_torch_utils         import convert_Dataset_to_Tensors



### ~~~
## ~~~ Config/setup
### ~~~

#
# ~~~ Template for what the dictionary of hyperparmeters should look like
hyperparameter_template = {
    #
    # ~~~ Misc.
    "DEVICE" : "cpu",
    "DTYPE" : "float",
    "SEED" : 2024,
    #
    # ~~~ Which problem
    "DATA" : "univar_missing_middle",
    "MODEL" : "univar_NN",
    #
    # ~~~ For training
    "OPTIMIZER" : "Adam",
    "LR" : 0.0005,
    "BATCH_SIZE" : 64,
    "N_EPOCHS" : 200,
    "EARLY_STOPPING" : True,
    "DELTA": 0.05,
    "PATIENCE" : 20,
    "STRIDE" : 30,
    "N_MC_SAMPLES" : 1,                     # ~~~ relevant for droupout
    #
    # ~~~ For visualization
    "MAKE_GIF" : True,
    "HOW_OFTEN" : 10,                       # ~~~ how many snap shots in total should be taken throughout training (each snap-shot being a frame in the .gif)
    "INITIAL_FRAME_REPETITIONS" : 24,       # ~~~ for how many frames should the state of initialization be rendered
    "FINAL_FRAME_REPETITIONS" : 48,         # ~~~ for how many frames should the state after training be rendered
    "HOW_MANY_INDIVIDUAL_PREDICTIONS" : 6,  # ~~~ how many posterior predictive samples to plot
    "VISUALIZE_DISTRIBUTION_USING_QUANTILES" : True, # ~~~ for dropout, if False, use mean +/- two standard deviatiations; if True, use empirical median and 95% quantile
    "N_POSTERIOR_SAMPLES" : 100,            # ~~~ for dropout, how many samples to use to make the empirical distributions for plotting
    #
    # ~~~ For metrics and visualization
    "N_POSTERIOR_SAMPLES_EVALUATION" : 1000,
    "SHOW_DIAGNOSTICS" : True,
    "SHOW_PLOT" : True
}

#
# ~~~ Define the variable `input_json_filename`
if hasattr(sys,"ps1"):
    #
    # ~~~ If this is an interactive (not srcipted) session, i.e., we are directly typing/pasting in the commands (I do this for debugging), then use the demo json name
    input_json_filename = "demo_nn.json"
    model_save_dir = None
    final_test = False
    overwrite_json = False
else:
    #
    # ~~~ Use argparse to extract the file name "my_hyperparmeters.json" from `python train_nn.py --json my_hyperparmeters.json` (https://stackoverflow.com/a/67731094)
    parser = argparse.ArgumentParser()
    try:
        parser.add_argument( '--json', type=str, required=True )
    except:
        print("")
        print("    Hint: try `python train_nn.py --json demo_nn`")
        print("")
        raise
    parser.add_argument( '--model_save_dir', type=str )
    parser.add_argument( '--final_test', action=argparse.BooleanOptionalAction )
    parser.add_argument( '--overwrite_json', action=argparse.BooleanOptionalAction )
    args = parser.parse_args()
    model_save_dir = args.model_save_dir
    final_test = (args.final_test is not None)
    overwrite_json = (args.overwrite_json is not None)
    input_json_filename = args.json
    input_json_filename = input_json_filename if input_json_filename.endswith(".json") else input_json_filename+".json"

#
# ~~~ Load the .json file into a dictionary
hyperparameters = json_to_dict(input_json_filename)

#
# ~~~ Load the dictionary's key/value pairs into the global namespace
globals().update(hyperparameters)       # ~~~ e.g., if hyperparameters=={ "a":1, "B":2 }, then this defines a=1 and B=2

#
# ~~~ Might as well fix a seed, e.g., for randomly shuffling the order of batches during training
torch.manual_seed(SEED)

#
# ~~~ Handle the dtypes not writeable in .json format (e.g., if your dictionary includes the value `torch.optim.Adam` you can't save it as .json)
DTYPE = getattr(torch,DTYPE)            # ~~~ e.g., DTYPE=="float" (str) -> DTYPE==torch.float (torch.dtype) 
torch.set_default_dtype(DTYPE)
Optimizer = getattr(optim,OPTIMIZER)    # ~~~ e.g., OPTIMIZER=="Adam" (str) -> Optimizer==optim.Adam

#
# ~~~ Load the data
try:
    data = import_module(f"bnns.data.{DATA}")   # ~~~ this is equivalent to `import bnns.data.<DATA> as data`
except:
    data = import_module(DATA)                  # ~~~ this is equivalent to `import <DATA> as data` (works if DATA.py is in the cwd or anywhere on the path)

D_train = set_Dataset_attributes( data.D_train, device=DEVICE, dtype=DTYPE )
D_test  =  set_Dataset_attributes( data.D_test, device=DEVICE, dtype=DTYPE )
D_val   =   set_Dataset_attributes( data.D_val, device=DEVICE, dtype=DTYPE ) # ~~~ for hyperparameter evaulation and such, use the validation set instead of the "true" test set
data_is_univariate = (D_train[0][0].numel()==1)
x_train, y_train   =   convert_Dataset_to_Tensors(D_train)
x_test,  y_test    =   convert_Dataset_to_Tensors(D_test if final_test else D_val)

try:
    grid = data.grid.to( device=DEVICE, dtype=DTYPE )
except:
    pass

#
# ~~~ Load the network architecture
try:
    model = import_module(f"bnns.models.{MODEL}")   # ~~~ this is equivalent to `import bnns.models.<MODEL> as model`
except:
    model = import_module(MODEL)                    # ~~~ this is equivalent to `import <MODEL> as model` (works if MODEL.py is in the cwd or anywhere on the path)

NN = model.NN.to( device=DEVICE, dtype=DTYPE )


#
# ~~~ Infer whether or not the model's forward pass is stochastic (e.g., whether or not it's using dropout)
X,_ = next(iter(torch.utils.data.DataLoader( D_train, batch_size=10 )))
with torch.no_grad():
    difference = NN(X)-NN(X)
    dropout = (difference.abs().mean()>0).item()



### ~~~
## ~~~ Train a conventional neural network, for reference
### ~~~

#
# ~~~ The optimizer, dataloader, and loss function
optimizer = Optimizer( NN.parameters(), lr=LR )
dataloader = torch.utils.data.DataLoader( D_train, batch_size=BATCH_SIZE )
loss_fn = nn.MSELoss()

#
# ~~~ Some naming stuff
description_of_the_experiment = "Conventional, Deterministic Training" if not dropout else "Conventional Training of a Neural Network with Dropout"

#
# ~~~ Some plotting stuff
if data_is_univariate:
    green_curve =  data.y_test.cpu().squeeze()
    x_train_cpu = data.x_train.cpu()
    y_train_cpu = data.y_train.cpu().squeeze()
    if dropout:
        #
        # ~~~ Override the plotting routine `plot_nn` by defining instead a routine which 
        plot_predictions = plot_bnn_empirical_quantiles if VISUALIZE_DISTRIBUTION_USING_QUANTILES else plot_bnn_mean_and_std
        def plot_nn( fig, ax, grid, green_curve, x_train_cpu, y_train_cpu, nn, extra_std=0., HOW_MANY_INDIVIDUAL_PREDICTIONS=HOW_MANY_INDIVIDUAL_PREDICTIONS, N_POSTERIOR_SAMPLES=N_POSTERIOR_SAMPLES, title=description_of_the_experiment ):
            #
            # ~~~ Draw from the predictive distribuion
            with torch.no_grad():
                predictions = torch.stack([ nn(grid) for _ in range(N_POSTERIOR_SAMPLES) ]).squeeze()
            return plot_predictions( fig, ax, grid, green_curve, x_train_cpu, y_train_cpu, predictions, extra_std, HOW_MANY_INDIVIDUAL_PREDICTIONS, title )
    #
    # ~~~ Plot the state of the model upon its initialization
    if MAKE_GIF:
        gif = GifMaker()      # ~~~ essentially just a list of images
        fig,ax = plt.subplots(figsize=(12,6))
        fig,ax = plot_nn( fig, ax, grid, green_curve, x_train_cpu, y_train_cpu, NN )
        for j in range(INITIAL_FRAME_REPETITIONS):
            gif.capture( clear_frame_upon_capture=(j+1==INITIAL_FRAME_REPETITIONS) )

#
# ~~~ Support N_EPOCHS to be a list of integers
try:
    CHECKPOINTS = list(N_EPOCHS)
except TypeError:
    CHECKPOINTS = [N_EPOCHS]
except:
    raise

assert isinstance(CHECKPOINTS,list)
for n_epochs in CHECKPOINTS:
    assert isinstance(n_epochs,int)
    assert n_epochs>=0

train_loss_curve = []
val_loss_curve = []
last_checkpoint = 0
starting_time = time()
first_round = True

decided_to_stop_early = False   # ~~~ not yet, anyway
if EARLY_STOPPING:
    early_stopper = EarlyStopper( delta=DELTA, patience=PATIENCE )

for n_epochs in CHECKPOINTS:
    if not decided_to_stop_early:
        with support_for_progress_bars():   # ~~~ this just supports green progress bars
            pbar = tqdm( desc=description_of_the_experiment, total=n_epochs, initial=last_checkpoint, ascii=' >=' )
            for e in range(n_epochs-last_checkpoint):
                # ~~~ 
                #
                ### ~~~
                ## ~~~ Main Loop
                ### ~~~
                #
                # ~~~ The actual training logic (totally conventional, hopefully familiar)
                if not decided_to_stop_early:
                    for X, y in dataloader:
                        X, y = X.to(DEVICE), y.to(DEVICE)
                        if dropout:
                            loss = 0.
                            for _ in range(N_MC_SAMPLES):
                                loss += loss_fn(NN(X),y)/N_MC_SAMPLES
                        else:
                            loss = loss_fn(NN(X),y)
                        loss.backward()
                        optimizer.step()
                        optimizer.zero_grad()
                        pbar.set_postfix({ "loss": f"{loss.item():<4.4f}" })
                        _ = pbar.update()
                        if (pbar.n+1)%HOW_OFTEN==0:
                            #
                            # ~~~ Plotting logic
                            if data_is_univariate and MAKE_GIF:
                                fig, ax = plot_nn( fig, ax, grid, green_curve, x_train_cpu, y_train_cpu, NN )
                                gif.capture()   # ~~~ save a picture of the current plot (whatever plt.show() would show)
                            #
                            # ~~~ Record a little diagnostic info
                            with torch.no_grad():
                                train_loss_curve.append( loss.item() )
                                val_loss_curve.append( loss_fn(NN(x_test),y_test).item() )
                                if EARLY_STOPPING:
                                    decided_to_stop_early = early_stopper(avg(val_loss_curve[-STRIDE:]))
                                    if decided_to_stop_early:
                                        break
            last_checkpoint = n_epochs
            pbar.close()
        # ~~~
        #
        ### ~~~
        ## ~~~ Metrics (evaluate the model at this checkpoint, and save the results)
        ### ~~~
        #
        # ~~~ Compute the posterior predictive distribution on the testing dataset
        predict = lambda points: torch.stack([ NN(points) for _ in range(N_POSTERIOR_SAMPLES_EVALUATION) ]) if dropout else NN(points)
        with torch.no_grad():
            predictions = predict(x_test)
        try:
            interpolary_grid = data.interpolary_grid.to( device=DEVICE, dtype=DTYPE )
            extrapolary_grid = data.extrapolary_grid.to( device=DEVICE, dtype=DTYPE )
            predictions_on_interpolary_grid = predict(interpolary_grid)
            predictions_on_extrapolary_grid = predict(extrapolary_grid)
        except AttributeError:
            my_warn(f"Could import `extrapolary_grid` or `interpolary_grid` from bnns.data.{data}. For the best assessment of the quality of the UQ, please define these variables in the data file (no labels necessary)")
        except:
            raise
        #
        # ~~~ Compute the desired metrics
        hyperparameters["n_epochs"] = pbar.n/len(dataloader)
        hyperparameters["compute_time"] = time() - starting_time
        hyperparameters["val_loss_curve"] = val_loss_curve
        hyperparameters["train_loss_curve"] = train_loss_curve
        if dropout:
            hyperparameters["METRIC_rmse_of_median"]             =      rmse_of_median( predictions, y_test )
            hyperparameters["METRIC_rmse_of_mean"]               =        rmse_of_mean( predictions, y_test )
            hyperparameters["METRIC_mae_of_median"]              =       mae_of_median( predictions, y_test )
            hyperparameters["METRIC_mae_of_mean"]                =         mae_of_mean( predictions, y_test )
            hyperparameters["METRIC_max_norm_of_median"]         =  max_norm_of_median( predictions, y_test )
            hyperparameters["METRIC_max_norm_of_mean"]           =    max_norm_of_mean( predictions, y_test )
            hyperparameters["METRIC_median_energy_score"]        =       energy_scores( predictions, y_test ).median().item()
            hyperparameters["METRIC_coverage"]                   =   aggregate_covarge( predictions, y_test, quantile_uncertainty=VISUALIZE_DISTRIBUTION_USING_QUANTILES )
            hyperparameters["METRIC_median_avg_inverval_score"]  =  avg_interval_score_of_response_features( predictions, y_test, quantile_uncertainty=VISUALIZE_DISTRIBUTION_USING_QUANTILES ).median().item()
            for use_quantiles in (True,False):
                show = SHOW_DIAGNOSTICS and (use_quantiles==VISUALIZE_DISTRIBUTION_USING_QUANTILES)  # ~~~ i.e., diagnostics are requesed, the prediction type mathces the uncertainty type (mean and std. dev., or median and iqr)
                tag = "quantile" if use_quantiles else "pm2_std"
                hyperparameters[f"METRIC_uncertainty_vs_accuracy_slope_{tag}"], hyperparameters[f"METRIC_uncertainty_vs_accuracy_cor_{tag}"]  =  uncertainty_vs_accuracy( predictions, y_test, quantile_uncertainty=VISUALIZE_DISTRIBUTION_USING_QUANTILES, quantile_accuracy=use_quantiles, show=show, verbose=SHOW_DIAGNOSTICS )
                try:
                    hyperparameters[f"METRIC_extrapolation_uncertainty_vs_proximity_slope_{tag}"], hyperparameters[f"METRIC_uncertainty_vs_proximity_cor_{tag}"]  =  uncertainty_vs_proximity( predictions_on_extrapolary_grid, use_quantiles, extrapolary_grid, x_train, show=show, title="Uncertainty vs Proximity to Data Outside the Region of Interpolation", verbose=SHOW_DIAGNOSTICS )
                    hyperparameters[f"METRIC_interpolation_uncertainty_vs_proximity_slope_{tag}"], hyperparameters[f"METRIC_uncertainty_vs_proximity_cor_{tag}"]  =  uncertainty_vs_proximity( predictions_on_interpolary_grid, use_quantiles, interpolary_grid, x_train, show=show, title="Uncertainty vs Proximity to Data Within the Region of Interpolation", verbose=SHOW_DIAGNOSTICS )
                    hyperparameters[f"METRIC_extrapolation_uncertainty_spread_{tag}"]  =  uncertainty_spread( predictions_on_extrapolary_grid, use_quantiles )
                    hyperparameters[f"METRIC_interpolation_uncertainty_spread_{tag}"]  =  uncertainty_spread( predictions_on_interpolary_grid, use_quantiles )
                except NameError:
                    pass    # ~~~ the user was already warned "Could import `extrapolary_grid` or `interpolary_grid` from bnns.data.{data}."
                except:
                    raise
        else:
            hyperparameters["METRIC_rmse"]      =      rmse( NN, x_test, y_test )
            hyperparameters["METRIC_mae"]       =       mae( NN, x_test, y_test )
            hyperparameters["METRIC_max_norm"]  =  max_norm( NN, x_test, y_test )
        #
        # ~~~ Save the results
        if input_json_filename.startswith("demo"):
            my_warn(f'Results are not saved when the hyperparameter json filename starts with "demo" (in this case `{input_json_filename}`)')
        else:
            #
            # ~~~ Put together the output json filename
            output_json_filename = input_json_filename if overwrite_json else generate_json_filename()
            if first_round:
                first_round = False
                if overwrite_json:
                    os.remove(input_json_filename)
            output_json_filename = process_for_saving(output_json_filename)
            hyperparameters["filname"] = output_json_filename
            #
            # ~~~ Ok, now actually save the results
            if model_save_dir is not None:
                model_save_path = os.path.join(
                        model_save_dir,
                        os.path.split(output_json_filename.strip(".json"))[1] + ".pth"
                    )
                hyperparameters["MODEL_SAVE_PATH"] = model_save_path
                torch.save(
                        NN.state_dict(),
                        model_save_path
                    )
            dict_to_json(
                    hyperparameters,
                    output_json_filename,
                    verbose = SHOW_DIAGNOSTICS
                )
        #
        # ~~~ Display the results
        if SHOW_DIAGNOSTICS:
            print_dict(hyperparameters)

#
# ~~~ Afterwards, develop the .gif or plot the trained model, if applicable
if data_is_univariate:
    if MAKE_GIF:
        for j in range(FINAL_FRAME_REPETITIONS):
            gif.frames.append( gif.frames[-1] )
        gif.develop( destination=description_of_the_experiment, fps=24 )
        plt.close()
    if SHOW_PLOT:
        fig,ax = plt.subplots(figsize=(12,6))
        fig, ax = plot_nn( fig, ax, grid, green_curve, x_train_cpu, y_train_cpu, NN )
        plt.show()

#
# ~~~ Validate implementation of the algorithm on the synthetic dataset "bivar_trivial"
if data.__name__ == "bnns.data.bivar_trivial" and SHOW_PLOT:
    from bnns.data.univar_missing_middle import x_test, y_test
    fig,ax = plt.subplots(figsize=(12,6))
    plt.plot( x_test.cpu(), y_test.cpu(), "--", color="green" )
    with torch.no_grad():
        y_pred = NN(data.D_test.X.to( device=DEVICE, dtype=DTYPE )).mean(dim=-1)
    plt.plot( x_test.cpu(), y_pred.cpu(), "-", color="blue" )
    fig.suptitle("If these lines roughly match, then the algorithm is surely working correctly")
    ax.grid()
    fig.tight_layout()
    plt.show()
